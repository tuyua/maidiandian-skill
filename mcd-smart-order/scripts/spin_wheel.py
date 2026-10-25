#!/usr/bin/env python3
"""麦当劳单品/套餐转盘：先按约束过滤，再随机抽取一个或多个结果。

用法：
    python spin_wheel.py --input wheel_input.json [--top 3] [--seed 12345]
    cat wheel_input.json | python spin_wheel.py

输入 JSON（金额单位：元）：
{
  "mode": "any",              // any=不限（默认）；combo=只要套餐；single=只要单品
  "budget": 30,               // 可选，单品价格上限
  "categories": ["主食"],      // 可选，限定分类
  "exclude_keywords": ["牛肉"],
  "max_kcal": 700,            // 可选，0 是合法值
  "weight_by_discount": true, // 可选，折扣力度大的中签概率更高
  "avoid": ["9900014838"],    // 可选，排除的餐品编码（例如上一轮已抽中）
  "items": [
    {"code": "9900014838", "name": "枫糖风味厚松饼猪柳蛋堡三件套",
     "price": 27.9, "origin_price": 39.5, "category": "主食",
     "tags": ["套餐"], "kcal": 560}
  ]
}

输出：JSON。结果是**随机抽取**，不代表最划算或最合适，展示时必须说明。
"""

import argparse
import json
import random
import sys

from mcd_common import InputError, as_int, normalize_tags, parse_kcal, parse_non_negative, resolve_category, to_cents

MAX_TOP = 10
MAX_ITEMS_LIMIT = 200
# 「勉强接受」的餐品仍可能被抽中，但中签权重降到这个比例
RELUCTANT_WEIGHT = 0.15

# 名称里出现这些词，即使没有 tags 也判定为套餐
COMBO_NAME_HINTS = ("套餐", "件套", "随心配", "乐园餐", "分享餐", "拼盘", "双人餐", "多人餐")


def classify(item, tags):
    """判定套餐还是单品：优先用菜单 tags，其次用名称关键词。"""
    if any(t in ("套餐", "超值套餐", "组合") for t in tags):
        return "combo"
    if "单品" in tags:
        return "single"
    name = str(item.get("name", ""))
    if any(hint in name for hint in COMBO_NAME_HINTS):
        return "combo"
    return "single"


def build_pool(payload, budget_cents, max_kcal, mode, categories, exclude, avoid, exclude_cats=None):
    items = payload.get("items")
    if not isinstance(items, list):
        raise InputError("items 必须是数组")
    if len(items) > MAX_ITEMS_LIMIT:
        raise InputError(f"items 数量不能超过 {MAX_ITEMS_LIMIT}")

    pool = []
    invalid = []
    non_food_count = 0
    exclude_cats = set(exclude_cats or [])
    seen = set()

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            invalid.append(f"#{idx} 不是对象")
            continue
        name = str(item.get("name", ""))
        code = str(item.get("code", "")).strip()

        if not code:
            invalid.append(f"{name or f'#{idx}'}：code 缺失")
            continue
        if code in seen:
            invalid.append(f"{name or code}：编码重复，已忽略")
            continue
        seen.add(code)

        # 忌口匹配必须同时覆盖名称与 tags
        tags = normalize_tags(item)
        haystack = name + " " + " ".join(tags)
        if any(kw in haystack for kw in exclude):
            continue
        if code in avoid:
            continue

        try:
            price_cents = to_cents(item.get("price"))
        except InputError as e:
            invalid.append(f"{name or code}：{e}")
            continue
        if price_cents == 0:
            continue
        if budget_cents is not None and price_cents > budget_cents:
            continue

        try:
            origin_cents = to_cents(item["origin_price"]) if item.get("origin_price") is not None else price_cents
        except InputError as e:
            invalid.append(f"{name or code}：{e}")
            origin_cents = price_cents
        origin_cents = max(origin_cents, price_cents)

        kcal, kcal_invalid = parse_kcal(item.get("kcal"))
        if kcal_invalid:
            invalid.append(f"{name or code}：热量数据非法，按缺失处理")
        # 设了热量上限时，缺失热量的餐品无法证明达标，不得进入候选
        if max_kcal is not None and kcal is None:
            continue
        if max_kcal is not None and kcal > max_kcal:
            continue

        # 非食品（周边、丝巾、玩具）与餐品共用菜单，且会混进食品分类；
        # 脚本侧确定性识别后直接丢弃，不依赖调用方是否传 exclude_categories
        category, is_non_food = resolve_category(item)
        if is_non_food:
            non_food_count += 1
            continue
        if categories and category not in categories:
            continue
        if category in exclude_cats:
            continue

        kind = classify(item, normalize_tags(item))
        if mode == "combo" and kind != "combo":
            continue
        if mode == "single" and kind != "single":
            continue

        pool.append(
            {
                "code": code,
                "name": name,
                "price": price_cents / 100,
                "origin_price": origin_cents / 100,
                "saved": (origin_cents - price_cents) / 100,
                "category": category,
                "kcal": kcal,
                "kind": kind,
                "tags": tags,
            }
        )

    return pool, invalid, non_food_count


def main():
    parser = argparse.ArgumentParser(description="麦当劳单品/套餐随机转盘")
    parser.add_argument("--input", help="输入 JSON 文件路径，缺省从 stdin 读取")
    parser.add_argument("--top", type=int, default=1, help="抽取数量，不重复")
    parser.add_argument("--seed", help="随机种子，传入后可复现同一结果")
    args = parser.parse_args()

    def fail(message, code):
        print(json.dumps({"error": message, "error_code": code}, ensure_ascii=False, indent=2))
        sys.exit(1)

    if args.top < 1 or args.top > MAX_TOP:
        fail(f"--top 必须在 1 到 {MAX_TOP} 之间", "invalid_param")

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

    if not isinstance(payload, dict):
        fail("输入必须是 JSON 对象", "bad_payload")
        return

    mode = payload.get("mode", "any")
    if mode not in ("any", "combo", "single"):
        fail("mode 只支持 any、combo 或 single", "invalid_param")
        return

    try:
        budget_cents = None
        if payload.get("budget") is not None:
            budget_cents = to_cents(payload.get("budget"))
            if budget_cents <= 0:
                raise InputError("budget 必须大于 0")
        max_kcal = None
        if payload.get("max_kcal") is not None:
            max_kcal = parse_non_negative(payload.get("max_kcal"), "max_kcal")
        top_n = as_int(args.top, "top", 1, MAX_TOP)
    except InputError as e:
        fail(str(e), "invalid_param")
        return

    categories = payload.get("categories")
    if categories is not None and not (isinstance(categories, list) and all(isinstance(c, str) for c in categories)):
        fail("categories 必须是字符串数组", "invalid_param")
        return

    exclude = payload.get("exclude_keywords") or []
    avoid = set(str(c) for c in (payload.get("avoid") or []))

    exclude_cats = payload.get("exclude_categories")
    if exclude_cats is not None and not (isinstance(exclude_cats, list) and all(isinstance(c, str) for c in exclude_cats)):
        fail("exclude_categories 必须是字符串数组", "invalid_param")
        return

    try:
        pool, invalid, non_food_count = build_pool(payload, budget_cents, max_kcal, mode, categories, exclude, avoid, exclude_cats)
    except InputError as e:
        fail(str(e), "invalid_item")
        return

    if not pool:
        fail("没有符合条件的餐品，请放宽预算、忌口或模式限制", "no_candidate")
        return

    weight_by_discount = bool(payload.get("weight_by_discount"))
    rng = random.Random(args.seed)

    soft_exclude = payload.get("soft_exclude_keywords") or []

    def reluctant_of(item):
        return sorted({kw for kw in soft_exclude if kw in item["name"] or any(kw in t for t in item["tags"])})

    remaining = list(pool)
    picked = []
    while remaining and len(picked) < top_n:
        # 「勉强接受」不排除，但大幅降低中签权重
        if soft_exclude:
            weights = []
            for item in remaining:
                base = 1.0 + item["saved"] if weight_by_discount else 1.0
                weights.append(base * (RELUCTANT_WEIGHT if reluctant_of(item) else 1.0))
            choice = rng.choices(remaining, weights=weights, k=1)[0]
        elif weight_by_discount:
            weights = [1.0 + item["saved"] for item in remaining]
            choice = rng.choices(remaining, weights=weights, k=1)[0]
        else:
            choice = rng.choice(remaining)
        picked.append({
            "code": choice["code"],
            "name": choice["name"],
            "price": choice["price"],
            "origin_price": choice["origin_price"],
            "saved": choice["saved"],
            "category": choice["category"],
            "kcal": choice["kcal"],
            "kind": choice["kind"],
            "reluctant_matches": reluctant_of(choice),
        })
        remaining.remove(choice)

    note = "随机抽取结果，已过滤不满足条件的餐品；不代表最划算或最合适"
    print(json.dumps(
        {
            "mode": mode,
            "pool_size": len(pool),
            "seed": args.seed,
            "weight_by_discount": weight_by_discount,
            "picked_count": len(picked),
            "picked": picked,
            # 脚本侧被判为非食品（周边）而剔除的条目数
            "non_food_excluded": non_food_count,
            "invalid_items": invalid,
            "price_basis": "menu_estimate",
            "note": note,
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
