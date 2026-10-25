#!/usr/bin/env python3
"""combo_optimizer.py 的离线回归测试。

全部使用本地样例数据，不调用任何麦当劳接口，不产生真实订单。
除场景断言外，还用一份独立实现的穷举基线核对排序结果。
"""

import json
import math
import os
import subprocess
import sys
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations_with_replacement
from pathlib import Path

SCRIPT = Path(__file__).parent / "combo_optimizer.py"
# 记录本测试自己创建的临时文件路径，只校验这些是否被清理；
# 直接 diff 系统临时目录会把其他进程创建的文件误判为泄漏
CREATED_TEMP_FILES = []

BASE = [
    {"code": "A1", "name": "板烧鸡腿堡三件套", "price": 27.9, "origin_price": 39.5, "category": "主食", "kcal": 620},
    {"code": "A2", "name": "麦辣鸡腿堡三件套", "price": 26.9, "origin_price": 41.0, "category": "主食", "kcal": 590},
    {"code": "A3", "name": "吉士汉堡套餐", "price": 20.5, "origin_price": 30.0, "category": "主食", "kcal": 480},
    {"code": "B1", "name": "薯条", "price": 12.0, "origin_price": 12.0, "category": "小食", "kcal": 330},
    {"code": "B2", "name": "麦乐鸡", "price": 14.0, "origin_price": 14.0, "category": "小食", "kcal": 290},
    {"code": "C1", "name": "可乐", "price": 9.5, "origin_price": 9.5, "category": "饮品", "kcal": 150},
    {"code": "C2", "name": "鲜萃咖啡", "price": 9.5, "origin_price": 9.5, "category": "饮品", "kcal": 20},
    {"code": "C3", "name": "无糖苏打水", "price": 8.0, "origin_price": 8.0, "category": "饮品", "kcal": 0},
    {"code": "D1", "name": "圆筒冰淇淋", "price": 5.0, "origin_price": 5.0, "category": "甜品", "kcal": 160},
    # 热量缺失，用于验证 kcal_verified
    {"code": "D2", "name": "新品派", "price": 8.5, "origin_price": 8.5, "category": "甜品"},
]


def to_cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def run(payload, expect_ok=True):
    """跑一次脚本。临时文件必定清理，子进程带超时并检查退出码。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    CREATED_TEMP_FILES.append(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", path],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    finally:
        if os.path.exists(path):
            os.remove(path)

    if proc.returncode != 0 and expect_ok:
        return {"_error": f"退出码 {proc.returncode}", "_stderr": proc.stderr}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_error": "输出不是合法 JSON", "_stderr": proc.stderr}


def brute_force(items, budget, people, max_items, max_kcal=None, goal="rich"):
    """独立实现的穷举基线：与脚本同规则，用于核对排序结果。"""
    budget_cents = to_cents(budget)
    best = None
    for size in range(1, max_items + 1):
        for combo in combinations_with_replacement(items, size):
            total = sum(to_cents(i["price"]) for i in combo)
            if total > budget_cents:
                continue
            staples = sum(1 for i in combo if i["category"] == "主食")
            if staples < people:
                continue
            kcal_values = [i.get("kcal") for i in combo]
            verified = all(k is not None for k in kcal_values)
            total_kcal = sum(k for k in kcal_values if k is not None) if verified else None
            if max_kcal is not None and (not verified or total_kcal > max_kcal):
                continue
            categories = len({i["category"] for i in combo})
            origin = sum(to_cents(i.get("origin_price", i["price"])) for i in combo)
            key = (-categories, -origin, -size, total) if goal == "rich" else (total, -categories, -size)
            if best is None or key < best[0]:
                best = (key, total, categories, origin, size)
    return best


def check(results, name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" -> {detail}"))


def plans_valid(results, r, budget, people, max_kcal=None, label=""):
    """检查所有返回方案都满足预算、人数与热量约束，而不只检查首个。"""
    ok = True
    for plan in r.get("top_plans", []):
        if to_cents(plan["total_price"]) > to_cents(budget):
            ok = False
        staples = sum(i["quantity"] for i in plan["items"] if i["category"] == "主食")
        if staples < people:
            ok = False
        if max_kcal is not None and (not plan["kcal_verified"] or plan["total_kcal"] > max_kcal):
            ok = False
    check(results, f"{label}所有返回方案均满足约束", ok)


def main():
    results = []

    # 1. 丰富优先：与独立穷举基线一致
    r = run({"budget": 40, "people": 1, "goal": "rich", "max_items": 3, "items": BASE})
    base_rich = brute_force(BASE, 40, 1, 3, goal="rich")
    top = r["top_plans"][0]
    check(results, "丰富优先：与穷举基线的类别数与金额一致",
          top["category_count"] == base_rich[2] and to_cents(top["total_price"]) == base_rich[1],
          f'{top["category_count"]}/{top["total_price"]} vs {base_rich[2]}/{base_rich[1] / 100}')
    plans_valid(results, r, 40, 1, label="丰富优先：")

    # 2. 低价优先：必须是满足约束中的最低价
    r2 = run({"budget": 40, "people": 1, "goal": "cheap", "max_items": 3, "items": BASE})
    base_cheap = brute_force(BASE, 40, 1, 3, goal="cheap")
    cheap = r2["top_plans"][0]
    check(results, "低价优先：与穷举基线的最低价一致",
          to_cents(cheap["total_price"]) == base_cheap[1],
          f'{cheap["total_price"]} vs {base_cheap[1] / 100}')
    plans_valid(results, r2, 40, 1, label="低价优先：")

    # 3. 多样性：主食+小食+饮品 优于 三份同类堆叠
    r3 = run({"budget": 60, "people": 1, "goal": "rich", "max_items": 3, "items": BASE})
    cats3 = r3["top_plans"][0]["category_count"]
    check(results, "丰富优先：类别多样性高于单纯堆叠", cats3 >= 3, cats3)

    # 4. 数量模型：预算足够时可买两份主食满足两人
    r4 = run({"budget": 60, "people": 2, "goal": "cheap", "max_items": 2, "items": BASE})
    ok4 = bool(r4.get("top_plans")) and any(
        sum(i["quantity"] for i in p["items"] if i["category"] == "主食") >= 2 for p in r4["top_plans"])
    check(results, "数量模型：两人可用两份主食满足", ok4, r4.get("error"))

    # 5. P0 人数硬约束：预算只够一份主食时不得给出有效推荐
    r5 = run({"budget": 25, "people": 2, "goal": "cheap", "max_items": 2, "items": BASE}, expect_ok=False)
    check(results, "P0 人数硬约束：预算不足时返回错误而非一人份方案",
          "error" in r5 and "top_plans" not in r5, r5.get("error"))

    # 5b. 分类名映射不上时同样报错，不得用“每人一件”糊弄过去
    mismatched = [dict(i, category="招牌推荐") for i in BASE]
    r5b = run({"budget": 80, "people": 2, "goal": "rich", "max_items": 3, "items": mismatched}, expect_ok=False)
    check(results, "P0 分类映射失败：明确报错而非放宽人数要求",
          "error" in r5b and "top_plans" not in r5b, r5b.get("error"))

    # 6. max_kcal=0：只允许零热量餐品通过
    zero_items = [
        {"code": "S0", "name": "零卡主食", "price": 15.0, "origin_price": 15.0, "category": "主食", "kcal": 0},
        {"code": "A1", "name": "板烧鸡腿堡三件套", "price": 27.9, "origin_price": 39.5, "category": "主食", "kcal": 620},
    ]
    r6 = run({"budget": 40, "people": 1, "goal": "cheap", "max_items": 2, "max_kcal": 0, "items": zero_items})
    ok6 = bool(r6.get("top_plans")) and all(
        p["kcal_verified"] and p["total_kcal"] == 0 for p in r6["top_plans"]) and all(
        i["code"] != "A1" for p in r6["top_plans"] for i in p["items"])
    check(results, "零热量上限：max_kcal=0 生效且排除正热量餐品", ok6, r6.get("error"))

    # 7. 热量缺失不得当成 0 或达标
    r7 = run({"budget": 40, "people": 1, "goal": "rich", "max_items": 3, "items": BASE})
    check(results, "热量缺失：标记为未核实",
          all((p["total_kcal"] is None) for p in r7["top_plans"] if not p["kcal_verified"]))

    # 8. 忌口过滤
    r8 = run({"budget": 40, "people": 1, "goal": "rich", "max_items": 3, "exclude_keywords": ["鸡"], "items": BASE})
    check(results, "忌口过滤：不出现含关键词餐品",
          all("鸡" not in i["name"] for p in r8["top_plans"] for i in p["items"]))

    # 9. 金额定点转换边界
    r9 = run({"budget": 10, "people": 1, "goal": "cheap", "max_items": 1,
              "items": [{"code": "X1", "name": "边界商品", "price": "1.005", "category": "主食"}]})
    check(results, "金额边界：1.005 定点进位为 1.01",
          bool(r9.get("top_plans")) and abs(r9["top_plans"][0]["total_price"] - 1.01) < 1e-9,
          r9.get("error") or r9["top_plans"][0]["total_price"])

    # 10. 非法入参返回结构化错误
    for label, payload in [
        ("budget 为 NaN", {"budget": float("nan"), "items": BASE[:1]}),
        ("budget 为负", {"budget": -10, "items": BASE[:1]}),
        ("people 为 0", {"budget": 40, "people": 0, "items": BASE[:1]}),
        ("goal 非法", {"budget": 40, "goal": "abc", "items": BASE[:1]}),
        ("max_items 为 0", {"budget": 40, "max_items": 0, "items": BASE[:1]}),
        ("重复编码", {"budget": 40, "items": [BASE[0], dict(BASE[0])]}),
        ("items 非数组", {"budget": 40, "items": "x"}),
    ]:
        res = run(payload, expect_ok=False)
        check(results, f"非法入参：{label} 返回结构化错误",
              "error" in res and "error_code" in res, res)

    # 11. 单品异常不中断整体
    r11 = run({"budget": 40, "people": 1, "goal": "cheap", "max_items": 2, "items": [
        {"code": "Z1", "name": "价格非法", "price": "abc", "category": "主食"},
        {"code": "Z2", "name": "热量为负", "price": 10, "category": "主食", "kcal": -5},
        BASE[2],
    ]})
    check(results, "单品异常：跳过并记录，不崩溃",
          bool(r11.get("top_plans")) and len(r11.get("invalid_items", [])) >= 2, r11.get("error") or r11.get("invalid_items"))

    # 12. 截断标记语义：恰好 40 个候选不得标记为截断
    many = [{"code": f"M{i}", "name": f"商品{i}", "price": 1.0 + i * 0.01, "category": "主食"} for i in range(40)]
    r12 = run({"budget": 30, "people": 1, "goal": "cheap", "max_items": 2, "items": many})
    check(results, "截断标记：恰好 40 个候选不误标",
          r12.get("candidate_count") == 40 and r12.get("candidate_truncated") is False, r12.get("candidate_truncated"))

    many41 = many + [{"code": "M40", "name": "商品40", "price": 1.5, "category": "主食"}]
    r13 = run({"budget": 30, "people": 1, "goal": "cheap", "max_items": 2, "items": many41})
    check(results, "截断标记：超过 40 个候选正确标记",
          r13.get("candidate_truncated") is True and r13.get("selected_count") <= 40, r13.get("candidate_truncated"))

    # 13. 临时文件清理：只检查本测试自己创建的路径
    leaked = [p for p in CREATED_TEMP_FILES if os.path.exists(p)]
    check(results, "测试临时文件已清理",
          not leaked and len(CREATED_TEMP_FILES) > 5, leaked or f"仅跟踪到 {len(CREATED_TEMP_FILES)} 个文件")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    if failed:
        print("失败项：", "、".join(failed))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
