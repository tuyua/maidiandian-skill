#!/usr/bin/env python3
"""三选一推荐视图：同一份需求，同时给出主推/省钱/健康三套方案并对比。

麦当劳点单的真实心理是「给我几个选择」，而不是一次只给一个最优解。本脚本在
combo_optimizer 上跑三组目标参数：
    - rich（主推，默认）：预算内尽量丰富、省得最多、品类最多样
    - cheap（省钱）：同样约束下尽量压低预估金额
    - lean（健康）：同样约束下优先低热量（需菜单有热量数据）

输出三张方案 + 差额对比 + 每张「为什么选它」的一句话理由，供 Agent 渲染成
对比卡片。所有金额仍是基于菜单价的**预估金额**，不是真实算价。

用法：
    python plan_compare.py --input payload.json [--top 3]
    cat payload.json | python plan_compare.py
"""

import argparse
import json
import sys

from combo_optimizer import InputError, plan_once

# 三种视图的展示元信息：key → (标题, 一句话定位)
VIEWS = [
    ("rich", "主推", "预算内尽量丰富、省得最多、品类最多样"),
    ("cheap", "省钱", "同样约束下尽量压低预估金额"),
    ("lean", "健康", "同样约束下优先低热量（需菜单有热量数据）"),
]

# goal → 中文标题，供 build_diff 等需要反查标题的场景使用
GOAL_TITLE = {goal: title for goal, title, _ in VIEWS}


def pick_goal(payload, goal, top_n):
    """跑单目标；无解或报错时返回含 error 的字典，不抛异常。"""
    trial = dict(payload)
    trial["goal"] = goal
    try:
        return plan_once(trial, top_n=top_n)
    except (InputError, ValueError) as e:
        return {"error": str(e), "error_code": "invalid_param"}


def plan_reason(goal, plan, reference_price):
    """为单张方案生成「为什么选它」的一句话理由（中文）。"""
    if goal == "rich":
        cats = plan.get("category_count", 0)
        saved = plan.get("saved", 0)
        if cats >= 3:
            if saved > 0.01:
                return f"品类最丰富（{cats} 类），还省下 ¥{saved:.2f}"
            return f"品类最丰富（{cats} 类），刚好花满预算"
        if saved > 0.01:
            return f"丰富优先，省得最多（¥{saved:.2f}），共 {plan.get('item_count')} 件"
        return f"丰富优先，共 {plan.get('item_count')} 件，刚好花满预算"
    if goal == "cheap":
        delta = reference_price - plan.get("total_price", 0)
        if delta > 0.001:
            return f"比主推更省 ¥{delta:.2f}，适合只求吃饱"
        return "已经是三套里最便宜的，性价比优先"
    # lean
    kcal = plan.get("total_kcal")
    if plan.get("kcal_verified") and kcal is not None:
        return f"热量最低（{kcal:.0f} kcal），适合控制热量"
    return "低热量优先，但本组合热量数据不全，仅供参考"


def build_diff(plans_by_goal, budget):
    """计算三套方案之间的价格/热量差额，并给出默认推荐。"""
    available = {g: p for g, p in plans_by_goal.items()
                 if "top_plans" in p and p.get("top_plans")}

    if not available:
        return {"available": [], "recommend": None, "why": "三套方案在当前约束下都无解"}

    # 每张方案取其 top1 作为代表
    reps = {g: p["top_plans"][0] for g, p in available.items()}

    prices = {g: r["total_price"] for g, r in reps.items()}
    kcal_info = {}
    for g, r in reps.items():
        if r.get("kcal_verified") and r.get("total_kcal") is not None:
            kcal_info[g] = r["total_kcal"]

    p_min = min(prices.values())
    p_max = max(prices.values())
    delta_price = round(p_max - p_min, 2)
    delta_kcal = None
    if len(kcal_info) >= 2:
        delta_kcal = round(max(kcal_info.values()) - min(kcal_info.values()), 0)

    # 默认推荐：优先主推（rich），其次省钱，最后健康
    recommend = "rich" if "rich" in available else (
        "cheap" if "cheap" in available else "lean")

    why = (f"默认推荐「{GOAL_TITLE[recommend]}」：三套预估价差 ¥{delta_price}"
           + (f"，热量差 {delta_kcal:.0f} kcal" if delta_kcal is not None else "")
           + f"，预算 {budget} 元。按需切换：省钱看价格、健康看热量。")

    return {
        "available": sorted(available.keys()),
        "price_spread": {"min": p_min, "max": p_max, "delta": delta_price},
        "kcal_spread": {"values": kcal_info, "delta": delta_kcal},
        "recommend": recommend,
        "why": why,
    }


def main():
    parser = argparse.ArgumentParser(description="麦当劳三选一推荐对比")
    parser.add_argument("--input", help="输入 JSON 文件路径，缺省从 stdin 读取")
    parser.add_argument("--top", type=int, default=3, help="每个视图返回的方案数")
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

    # 预算等基础校验交给 plan_once，但这里先抓缺 budget 这种全局错误
    try:
        budget = float(payload.get("budget"))
    except (TypeError, ValueError):
        fail("budget 缺失或非法", "invalid_param")
        return

    plans_by_goal = {}
    for goal, title, _desc in VIEWS:
        res = pick_goal(payload, goal, args.top)
        plans_by_goal[goal] = res

    rich_plan = (plans_by_goal.get("rich", {}).get("top_plans") or [{}])[0]
    reference_price = rich_plan.get("total_price", budget) if rich_plan else budget

    views = []
    for goal, title, desc in VIEWS:
        res = plans_by_goal[goal]
        if "top_plans" in res and res.get("top_plans"):
            plan = res["top_plans"][0]
            views.append({
                "goal": goal,
                "title": title,
                "desc": desc,
                "reason": plan_reason(goal, plan, reference_price),
                "plan": plan,
                "truncated": res.get("truncated", False),
                "non_food_excluded": res.get("non_food_excluded", 0),
            })
        else:
            views.append({
                "goal": goal,
                "title": title,
                "desc": desc,
                "reason": "当前约束下无解（" + str(res.get("error_code", "no_plan")) + "）",
                "plan": None,
                "error": res.get("error"),
            })

    diff = build_diff(plans_by_goal, budget)

    output = {
        "budget": budget,
        "people": payload.get("people", 1),
        "price_basis": "menu_estimate",
        "views": views,
        "diff": diff,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
