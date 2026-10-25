#!/usr/bin/env python3
"""麦当劳预算组合优化：在预算内枚举餐品组合，按目标输出推荐方案。

用法：
    python combo_optimizer.py --input combo_input.json [--top 5]
    cat combo_input.json | python combo_optimizer.py

输入 JSON（金额单位：元，脚本内部按“分”定点计算）：
{
  "budget": 30,
  "people": 1,
  "goal": "rich",              // rich=预算内吃得更丰富（默认）；cheap=尽量少花钱
  "max_items": 4,              // 组合总份数上限
  "require_categories": ["主食"],
  "exclude_keywords": ["牛肉"],
  "max_kcal": 700,             // 0 是合法值
  "items": [
    {"code": "9900014838", "name": "枫糖风味厚松饼猪柳蛋堡三件套",
     "price": 27.9, "origin_price": 39.5, "category": "主食",
     "kcal": 560, "max_quantity": 2}
  ]
}

输出：JSON。所有金额是基于菜单价的**预估金额**，不是真实算价结果。
"""

import argparse
import json
import math
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import combinations_with_replacement

from mcd_common import InputError, as_int, resolve_category, to_cents

MAX_CANDIDATES = 40
MAX_ENUMERATIONS = 400000
MAX_ITEMS_LIMIT = 8
TRIM_FACTOR = 4
KEEP_PER_CATEGORY = 3
KEEP_CHEAPEST = 5
VALID_GOALS = ("rich", "cheap", "lean")
DEFAULT_STAPLE = "主食"


def validate_payload(payload):
    """集中校验入参。任何一项不合法都直接报错，不做静默修正。"""
    if not isinstance(payload, dict):
        raise InputError("输入必须是 JSON 对象")

    budget_cents = to_cents(payload.get("budget"))
    if budget_cents <= 0:
        raise InputError("budget 必须大于 0")

    people = as_int(payload.get("people", 1), "people", 1, 20)
    max_items = as_int(payload.get("max_items", 4), "max_items", 1, MAX_ITEMS_LIMIT)

    goal = payload.get("goal", "rich")
    if goal not in VALID_GOALS:
        raise InputError(f"goal 仅支持 {' / '.join(VALID_GOALS)}")

    # 用 is not None 判断，保证 max_kcal=0 生效
    max_kcal = None
    if payload.get("max_kcal") is not None:
        if isinstance(payload.get("max_kcal"), bool):
            raise InputError("max_kcal 必须是数字")
        try:
            max_kcal = float(payload.get("max_kcal"))
        except (TypeError, ValueError):
            raise InputError("max_kcal 必须是数字")
        if not math.isfinite(max_kcal) or max_kcal < 0:
            raise InputError("max_kcal 必须是有限非负数")

    require = payload.get("require_categories")
    if require is None:
        require = [DEFAULT_STAPLE]
    elif isinstance(require, list) and all(isinstance(x, str) and x for x in require):
        require = list(require)
    else:
        raise InputError("require_categories 必须是非空字符串数组")

    exclude = payload.get("exclude_keywords")
    if exclude is not None and not (isinstance(exclude, list) and all(isinstance(x, str) for x in exclude)):
        raise InputError("exclude_keywords 必须是字符串数组")

    # 整类排除：真实菜单里混有非食品（周边、丝巾、玩具），它们与餐品同菜单、有价格，
    # 且会被嵌在「人气热卖」「小食甜品/其他」等食品分类里。
    # 因此脚本侧做确定性识别（见 mcd_common.resolve_category）：命中非食品规则的一律
    # 归为「周边」并直接丢弃，不依赖调用方是否传了 exclude_categories。
    exclude_cats = payload.get("exclude_categories")
    if exclude_cats is None:
        exclude_cats = []
    elif isinstance(exclude_cats, list) and all(isinstance(x, str) and x for x in exclude_cats):
        exclude_cats = list(exclude_cats)
    else:
        raise InputError("exclude_categories 必须是非空字符串数组")

    # 「勉强接受」级忌口：不排除，但方案会打标记并排到后面
    soft = payload.get("soft_exclude_keywords")
    if soft is None:
        soft = []
    elif isinstance(soft, list) and all(isinstance(x, str) for x in soft):
        soft = list(soft)
    else:
        raise InputError("soft_exclude_keywords 必须是字符串数组")

    items = payload.get("items")
    if not isinstance(items, list):
        raise InputError("items 必须是数组")

    return {
        "budget_cents": budget_cents,
        "people": people,
        "max_items": max_items,
        "goal": goal,
        "max_kcal": max_kcal,
        "require": require,
        "require_explicit": payload.get("require_categories") is not None,
        "soft_exclude": soft,
        "exclude_categories": set(exclude_cats),
    }


class ItemError(InputError):
    """单条餐品数据不合法（区别于入参整体不合法）。"""


def build_candidates(payload, params):
    exclude = payload.get("exclude_keywords") or []
    exclude_cats = params.get("exclude_categories") or set()
    budget_cents = params["budget_cents"]
    default_qty = params["max_items"]

    candidates = []
    invalid = []
    non_food_count = 0
    seen_codes = set()

    for idx, item in enumerate(payload.get("items") or []):
        if not isinstance(item, dict):
            invalid.append(f"#{idx} 不是对象")
            continue

        name = item.get("name")
        name = str(name) if isinstance(name, str) else ""
        code = item.get("code")
        code = str(code).strip() if isinstance(code, str) else ""
        # 忌口匹配必须同时覆盖名称与 tags：只含「100%纯牛肉」标签的餐品名称里没有「牛肉」
        tags = [str(t) for t in (item.get("tags") or []) if isinstance(t, str)]
        haystack = name + " " + " ".join(tags)

        if not code:
            invalid.append(f"{name or f'#{idx}'}：code 缺失或不是非空字符串")
            continue
        # 同编码重复出现属于数据重复，直接报错，避免靠重复记录伪造份数
        if code in seen_codes:
            raise ItemError(f"重复餐品编码：{code}，请合并为一条并使用 max_quantity 限量")
        seen_codes.add(code)

        if any(kw in haystack for kw in exclude):
            continue

        # 非食品（周边）无论被标成什么分类都直接丢弃：宁可少选，也不能推荐非餐品
        category, is_non_food = resolve_category(item)
        if is_non_food:
            non_food_count += 1
            continue
        if category in exclude_cats:
            continue

        try:
            price_cents = to_cents(item.get("price"))
        except InputError as e:
            invalid.append(f"{name or code}：{e}")
            continue
        if price_cents == 0:
            continue
        if price_cents > budget_cents:
            continue

        try:
            origin_cents = to_cents(item["origin_price"]) if item.get("origin_price") is not None else price_cents
        except InputError as e:
            invalid.append(f"{name or code}：{e}")
            origin_cents = price_cents
        origin_cents = max(origin_cents, price_cents)

        kcal = item.get("kcal")
        if kcal is not None:
            try:
                kcal_value = float(kcal) if not isinstance(kcal, bool) else float("nan")
            except (TypeError, ValueError):
                kcal_value = float("nan")
            if not math.isfinite(kcal_value) or kcal_value < 0:
                invalid.append(f"{name or code}：热量数据非法，按缺失处理")
                kcal = None
            else:
                kcal = kcal_value

        max_quantity = item.get("max_quantity", default_qty)
        try:
            max_quantity = as_int(max_quantity, "max_quantity", 0, MAX_ITEMS_LIMIT)
        except InputError as e:
            invalid.append(f"{name or code}：{e}，按最大份数 {default_qty} 处理")
            max_quantity = default_qty
        if max_quantity == 0:
            continue

        candidates.append(
            {
                "idx": idx,
                "code": code,
                "name": name,
                "price_cents": price_cents,
                "origin_cents": origin_cents,
                "category": category,
                "kcal": kcal,
                "max_quantity": max_quantity,
                "tags": tags,
            }
        )

    return candidates, invalid, non_food_count


def select_candidates(candidates, require, max_kcal=None):
    """候选过多时截断，但先保留必选分类与低价可行项，避免丢掉唯一可行解。"""
    if len(candidates) <= MAX_CANDIDATES:
        return candidates, False

    keep = []
    kept = set()

    def add(item):
        if item["idx"] not in kept:
            kept.add(item["idx"])
            keep.append(item)

    # 设了热量上限时，先按分类保住「热量已核实且不超限」的餐品：
    # 只按价格取前几名会把可核实的餐品全裁掉（真实菜单里便宜的随心配/新品恰好都没营养数据），
    # 结果明明有可行组合却报「未找到可行组合」，还会误导去改分类映射
    if max_kcal is not None:
        for category in set(require) | {DEFAULT_STAPLE}:
            feasible = sorted(
                [c for c in candidates
                 if c["category"] == category and c["kcal"] is not None and c["kcal"] <= max_kcal],
                key=lambda x: x["price_cents"])
            for item in feasible[:KEEP_PER_CATEGORY]:
                add(item)

    for category in set(require) | {DEFAULT_STAPLE}:
        same = sorted([c for c in candidates if c["category"] == category], key=lambda x: x["price_cents"])
        for item in same[:KEEP_PER_CATEGORY]:
            add(item)

    for item in sorted(candidates, key=lambda x: x["price_cents"])[:KEEP_CHEAPEST]:
        add(item)

    for item in sorted(candidates, key=lambda x: (-(x["origin_cents"] - x["price_cents"]), x["price_cents"])):
        if len(keep) >= MAX_CANDIDATES:
            break
        add(item)

    return keep[:MAX_CANDIDATES], True


def rich_key(plan):
    # 丰富优先：先避开「勉强接受」的餐品，再看类别多样性、价值、份数
    return (plan["has_reluctant"], -plan["category_count"], -plan["total_origin"], -plan["item_count"], plan["total_price"])


def cheap_key(plan):
    # 低价优先：先避开「勉强接受」的餐品，再看预估金额
    return (plan["has_reluctant"], plan["total_price"], -plan["category_count"], -plan["item_count"])


def lean_key(plan):
    """热量优先：先看有没有「勉强接受」，再看热量是否可核实，最后比热量低者获胜。"""
    return (plan["has_reluctant"], not plan["kcal_verified"],
            plan["total_kcal"] if plan["total_kcal"] is not None else float("inf"),
            plan["total_price"])


KEY_FUNCS = {"cheap": cheap_key, "lean": lean_key, "rich": rich_key}


def search(candidates, params, top_n):
    budget_cents = params["budget_cents"]
    people = params["people"]
    max_items = params["max_items"]
    max_kcal = params["max_kcal"]
    require = params["require"]
    staple_categories = set(require)
    soft_exclude = params["soft_exclude"]
    key_func = KEY_FUNCS[params["goal"]]

    best = []
    enumerations = 0
    search_truncated = False

    for size in range(1, max_items + 1):
        for combo in combinations_with_replacement(candidates, size):
            enumerations += 1
            if enumerations > MAX_ENUMERATIONS:
                search_truncated = True
                break

            quantities = {}
            for item in combo:
                quantities[item["idx"]] = quantities.get(item["idx"], 0) + 1
            if any(quantities[item["idx"]] > item["max_quantity"] for item in combo):
                continue

            total_price = sum(item["price_cents"] for item in combo)
            if total_price > budget_cents:
                continue

            # 人数是硬约束：必选分类的份数必须满足人数，任何情况下都不放宽
            staple_count = sum(qty for idx, qty in quantities.items() if candidates_by_idx[idx]["category"] in staple_categories)
            if staple_count < people:
                continue

            categories = {item["category"] for item in combo}
            if not all(req in categories for req in require):
                continue

            kcal_values = [item["kcal"] for item in combo]
            kcal_verified = all(k is not None for k in kcal_values)
            total_kcal = sum(k for k in kcal_values if k is not None) if kcal_verified else None
            if max_kcal is not None and (not kcal_verified or total_kcal > max_kcal):
                continue

            total_origin = sum(item["origin_cents"] for item in combo)
            # 「勉强接受」不排除，但整份方案打标记并排到后面
            reluctant_matches = sorted(
                {kw for item in combo for kw in soft_exclude
                 if kw in item["name"] or any(kw in t for t in item.get("tags", []))}
            )
            grouped = {}
            for item in combo:
                entry = grouped.setdefault(
                    item["idx"],
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "price": item["price_cents"] / 100,
                        "origin_price": item["origin_cents"] / 100,
                        "category": item["category"],
                        "kcal": item["kcal"],
                        "quantity": 0,
                    },
                )
                entry["quantity"] += 1

            best.append(
                {
                    "items": list(grouped.values()),
                    "total_price": total_price / 100,
                    "total_origin": total_origin / 100,
                    "saved": (total_origin - total_price) / 100,
                    "total_kcal": total_kcal,
                    "kcal_verified": kcal_verified,
                    "remaining": (budget_cents - total_price) / 100,
                    "item_count": size,
                    "category_count": len(categories),
                    "has_reluctant": 1 if reluctant_matches else 0,
                    "reluctant_matches": reluctant_matches,
                }
            )
            # 只保留有限规模的最优方案，避免全量存储导致内存增长
            if len(best) >= top_n * TRIM_FACTOR * 4:
                best = sorted(best, key=key_func)[: top_n * TRIM_FACTOR]
        if search_truncated:
            break

    return sorted(best, key=key_func)[:top_n], search_truncated


def _no_candidate_reason(payload, params):
    """候选为空时说清楚原因：是预算买不起，还是被忌口/分类挡光了。"""
    budget = params["budget_cents"] / 100
    cheapest = None
    blocked_keywords = 0
    blocked_category = 0
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        category, is_non_food = resolve_category(item)
        if is_non_food:
            continue
        name = str(item.get("name", ""))
        tags = [str(t) for t in (item.get("tags") or []) if isinstance(t, str)]
        haystack = name + " " + " ".join(tags)
        if any(kw in haystack for kw in (payload.get("exclude_keywords") or [])):
            blocked_keywords += 1
            continue
        if category in params.get("exclude_categories") or set():
            blocked_category += 1
            continue
        try:
            cents = to_cents(item.get("price"))
        except InputError:
            continue
        if cents <= 0:
            continue
        if cheapest is None or cents < cheapest[0]:
            cheapest = (cents, name)

    if cheapest is None:
        return "菜单里没有可用餐品（全部被忌口或分类过滤），请放宽忌口或检查分类映射"

    low_price, low_name = cheapest[0], cheapest[1]
    extra = []
    if blocked_keywords:
        extra.append(f"{blocked_keywords} 项被忌口过滤")
    if blocked_category:
        extra.append(f"{blocked_category} 项被 exclude_categories 过滤")
    tail = f"（另有 {'、'.join(extra)}）" if extra else ""
    return (f"预算 {budget} 元买不到任何餐品：菜单里最便宜的是「{low_name}」，"
            f"要 {low_price / 100:.2f} 元{tail}。请提高预算或放宽忌口")


def plan_once(payload, top_n=5, allow_diagnose=True):
    """跑一次完整流程并返回结果字典（不打印、不退出），供 main 与 plan_compare 复用。

    有解时返回（字典）：budget / goal / candidate_count / truncated / top_plans 等。
    无解且 allow_diagnose 为真时返回诊断字典：error + error_code + nearest_plan
    + shortfall + relaxed，告诉用户「差多少钱、差在哪个约束」。
    """
    params = validate_payload(payload)
    candidates, invalid, non_food_count = build_candidates(payload, params)

    if not candidates:
        return {
            "error": _no_candidate_reason(payload, params),
            "error_code": "no_candidate",
            "candidate_count": 0,
            "non_food_excluded": non_food_count,
            "invalid_items": invalid,
            "price_basis": "menu_estimate",
        }

    global candidates_by_idx
    candidates_by_idx = {c["idx"]: c for c in candidates}

    candidate_truncated = len(candidates) > MAX_CANDIDATES
    selected, _ = select_candidates(candidates, params["require"], params["max_kcal"])
    plans, search_truncated = search(selected, params, top_n)

    if not plans:
        if allow_diagnose:
            return diagnose(payload, params, candidates, invalid, non_food_count, top_n)
        return {
            "error": "没有满足约束的组合",
            "error_code": "no_plan",
            "candidate_count": len(candidates),
            "non_food_excluded": non_food_count,
            "invalid_items": invalid,
        }

    return {
        "budget": params["budget_cents"] / 100,
        "goal": params["goal"],
        "people": params["people"],
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "candidate_truncated": candidate_truncated,
        "search_truncated": search_truncated,
        # 任一截断发生时，结果只能称“已比较方案中更优”
        "truncated": candidate_truncated or search_truncated,
        # 脚本侧被判为非食品（周边）而剔除的条目数，用于核对菜单是否被污染
        "non_food_excluded": non_food_count,
        "invalid_items": invalid,
        "price_basis": "menu_estimate",
        "top_plans": plans,
    }


def cheapest_plan(result):
    """从结果里挑预估金额最低的方案（记忆容易的最小白银 Bias 排序时用取插队）。"""
    plans = (result or {}).get("top_plans") or []
    if not plans:
        return None
    return min(plans, key=lambda p: p["total_price"])


def diagnose(payload, params, candidates, invalid, non_food_count, top_n):
    """无解时逐级放宽约束，找出「到底卡在哪一条」，给出最接近的可行组合与差额。

    放宽顺序：预算 → 热量上限 → 分类要求。每一步都会在 relaxed 里写明放宽了什么，
    绝不静默降级：error_code 会明确区分缺钱、热量冲突还是分类冲突。
    """
    base = {
        "candidate_count": len(candidates),
        "non_food_excluded": non_food_count,
        "invalid_items": invalid,
        "price_basis": "menu_estimate",
    }

    def drop_budget(trial):
        # 松开预算：上限取「全部原始条目都买得起」，注意候选列表已被预算过滤过，
        # 不能用过滤后的 candidates 求和，否则抬高后的预算仍然买不起主食
        cap = 0
        for item in payload.get("items") or []:
            try:
                cap += to_cents(item.get("price"))
            except InputError:
                continue
        trial["budget"] = cap / 100 + 1
        trial["goal"] = "cheap"

    def drop_kcal(trial):
        trial.pop("max_kcal", None)

    def simplify_require(trial):
        trial["require_categories"] = [DEFAULT_STAPLE]

    steps = [("budget", drop_budget), ("max_kcal", drop_kcal), ("require_categories", simplify_require)]

    for depth in range(1, len(steps) + 1):
        relaxed = [name for name, _ in steps[:depth]]

        def chain(trial, seq=[fn for _, fn in steps[:depth]]):
            for apply_one in seq:
                apply_one(trial)

        trial = dict(payload)
        chain(trial)
        try:
            res = plan_once(trial, top_n=top_n, allow_diagnose=False)
        except InputError:
            continue

        plan = cheapest_plan(res)
        if not plan:
            continue

        budget = params["budget_cents"] / 100
        shortfall = round(plan["total_price"] - budget, 2)
        names = " + ".join(f"{i['name']}×{i['quantity']}" if i["quantity"] > 1 else i["name"]
                           for i in plan["items"])

        blocks = []
        if shortfall > 0:
            blocks.append(f"预算还差 {shortfall} 元")
        if "max_kcal" in relaxed and payload.get("max_kcal") is not None:
            blocks.append("热量上限在这个预算下凑不出来")
        if "require_categories" in relaxed:
            blocks.append(f"分类要求在当前菜单下凑不齐，放宽到只要求「{DEFAULT_STAPLE}」才可行")
        if not blocks:
            blocks.append("约束互相冲突")

        if "require_categories" in relaxed:
            error_code = "category_conflict"
        elif "max_kcal" in relaxed:
            error_code = "kcal_conflict"
        else:
            error_code = "budget_shortage"

        hint = (f"最接近的可行组合：{names}，共 {plan['total_price']} 元；"
                + "；".join(blocks)
                + f"。为了让它有解，已临时放宽：{'、'.join(relaxed)}")

        return dict(base, **{
            "error": hint,
            "error_code": error_code,
            "shortfall": shortfall,
            "relaxed": relaxed,
            "nearest_plan": plan,
            "hint": "下一步可选：提高预算 / 调整上面 relaxed 列出的约束 / 减少点数（people）",
        })

    # 先查「原始菜单里根本不存在这个分类」：这种情况放宽多少都没用，直接说清楚
    raw_categories = set()
    for item in payload.get("items") or []:
        if isinstance(item, dict):
            category, is_non_food = resolve_category(item)
            if not is_non_food:
                raw_categories.add(category)
    missing = [cat for cat in params["require"] if cat not in raw_categories]
    if missing:
        return dict(base, **{
            "error": (f"菜单里没有任何餐品被映射到分类「{'、'.join(missing)}」，放宽预算也没用。"
                      "请修正 category 映射（麦当劳原分类需映射到 主食/小食/饮品/甜品）"),
            "error_code": "no_plan",
        })

    required = "、".join(params["require"])
    if params["people"] > 1:
        reason = (f"预算内无法购齐 {params['people']} 份「{required}」，"
                  "请提高预算、减少人数，或放宽热量/忌口")
    else:
        reason = f"未找到含「{required}」的可行组合，请检查 category 分类映射或放宽限制"
    return dict(base, **{"error": reason, "error_code": "no_plan"})


def main():
    parser = argparse.ArgumentParser(description="麦当劳预算内组合优化")
    parser.add_argument("--input", help="输入 JSON 文件路径，缺省从 stdin 读取")
    parser.add_argument("--top", type=int, default=5, help="返回的方案数量")
    args = parser.parse_args()

    def fail(message, code):
        print(json.dumps({"error": message, "error_code": code}, ensure_ascii=False, indent=2))
        sys.exit(1)

    if args.top < 1:
        fail("--top 必须大于等于 1", "invalid_param")

    try:
        if args.input:
            with open(args.input, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = json.load(sys.stdin)
    except OSError as e:
        fail(f"输入文件读取失败：{e}", "bad_input_file")
        return
    except json.JSONDecodeError as e:
        fail(f"输入不是合法 JSON：{e}", "bad_input_file")
        return

    try:
        result = plan_once(payload, top_n=args.top)
    except ItemError as e:
        fail(str(e), "invalid_item")
        return
    except InputError as e:
        fail(str(e), "invalid_param")
        return

    if "error" in result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
