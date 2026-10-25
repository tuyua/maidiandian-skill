#!/usr/bin/env python3
"""必需用例回归：把「缺输入 / 边界值 / 可复现 / 完整任务」四类必需检查固化下来。

这些用例对应测试规范里必须由实现方自证的必需项，全程离线、只跑本地样例数据，
不调用任何麦当劳接口，也不触碰用户真实档案 ~/.workbuddy/mcd-profile.json。

本套件此前一度只在临时目录里存在、未落盘回流，导致报告里的数字无法复现；
现在作为 scripts/test_required_cases.py 纳入 run_tests.py 的自动发现。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
OPTIMIZER = HERE / "combo_optimizer.py"
WHEEL = HERE / "spin_wheel.py"
RANK = HERE / "rank_by_actual.py"
PROFILE = HERE / "profile.py"

BASE = [
    {"code": "M1", "name": "巨无霸", "price": 22.5, "category": "主食", "tags": ["100%纯牛肉"], "kcal": 503},
    {"code": "M2", "name": "麦香鸡", "price": 17.5, "category": "主食", "kcal": 400},
    {"code": "M3", "name": "麦辣鸡腿堡", "price": 22.5, "category": "主食", "tags": ["辣"], "kcal": 485},
    {"code": "S1", "name": "薯条", "price": 12.0, "category": "小食", "kcal": 289},
    {"code": "S2", "name": "脆薯饼", "price": 8.0, "category": "小食", "kcal": 230},
    {"code": "D1", "name": "圆筒冰淇淋", "price": 5.0, "category": "甜品", "kcal": 93},
    {"code": "N1", "name": "麦当劳限定丝巾", "price": 34.9, "category": "周边"},
    # 伪装成小食的非食品：模拟真实 query-meals 把周边嵌进食品分类的形态（P6）
    {"code": "N2", "name": "薯条脆卜卜毛绒周边", "price": 24.9, "category": "小食"},
]

NON_FOOD = {"N1", "N2"}


def run(script, payload, extra=None):
    """执行脚本，返回 (解析后的 JSON, 退出码, stderr)。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run([sys.executable, str(script), "--input", path] + (extra or []),
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        try:
            return json.loads(proc.stdout or "{}"), proc.returncode, proc.stderr or ""
        except json.JSONDecodeError:
            return {"_raw": proc.stdout}, proc.returncode, proc.stderr or ""
    finally:
        if os.path.exists(path):
            os.remove(path)


def main():
    results = []

    def check(name, ok, extra=""):
        results.append((name, ok, extra))

    # --- 缺输入：一律结构化报错并以非 0 退出码结束，不得静默兜底 ---
    missing, rc, err = run(OPTIMIZER, {"people": 1, "items": BASE})
    check("缺 budget：返回结构化错误且退出码非 0",
          missing.get("error_code") == "invalid_param" and rc == 1 and "Traceback" not in err,
          f"rc={rc} body={missing.get('error_code') or missing}")

    bad_items, rc, _ = run(OPTIMIZER, {"budget": 30, "items": "不是数组"})
    check("items 非数组：报错且不崩溃", bad_items.get("error_code") == "invalid_param" and rc == 1, bad_items)

    negative, rc, _ = run(OPTIMIZER, {"budget": -5, "items": BASE})
    check("budget 为负数：直接拒绝", negative.get("error_code") == "invalid_param" and rc == 1, negative)

    zero_people, rc, _ = run(OPTIMIZER, {"budget": 30, "people": 0, "items": BASE})
    check("people=0：拒绝而不是按 1 人处理", zero_people.get("error_code") == "invalid_param" and rc == 1, zero_people)

    bad_goal, rc, _ = run(OPTIMIZER, {"budget": 30, "goal": "best", "items": BASE})
    check("goal 非法：拒绝", bad_goal.get("error_code") == "invalid_param" and rc == 1, bad_goal)

    dup, rc, _ = run(OPTIMIZER, {"budget": 30, "items": BASE + [dict(BASE[1])]})
    check("重复编码：拒绝，禁止靠重复记录伪造份数",
          dup.get("error_code") == "invalid_item" and rc == 1, dup)

    # --- 边界值：0 是合法输入，必须给出结构化结论而不是崩溃 ---
    zero_kcal, rc, err = run(OPTIMIZER, {"budget": 30, "max_kcal": 0, "items": BASE})
    # max_kcal=0 时所有餐品热量都 > 0，诊断会给出更精确的 kcal_conflict（带最近方案），
    # 而不是笼统的 no_plan；核心是「结构化错误、不崩溃、退出码 1」
    check("max_kcal=0：视为合法边界并返回结构化无解",
          rc == 1 and zero_kcal.get("error_code") in
          ("no_plan", "no_candidate", "kcal_conflict", "budget_shortage", "category_conflict")
          and "Traceback" not in err,
          f"rc={rc} body={zero_kcal.get('error_code') or zero_kcal}")

    # --- 转盘输入校验 ---
    bad_mode, rc, _ = run(WHEEL, {"mode": "random", "items": BASE})
    check("转盘 mode 非法：报错", bad_mode.get("error_code") == "invalid_param" and rc == 1, bad_mode)

    over_top, rc, _ = run(WHEEL, {"items": BASE}, extra=["--top", "11"])
    check("转盘 --top 越界：报错", over_top.get("error_code") == "invalid_param" and rc == 1, over_top)

    # --- 随机可复现：同种子必得同结果，不同种子应有差异 ---
    same1, _, _ = run(WHEEL, {"items": BASE}, extra=["--seed", "20261024", "--top", "3"])
    same2, _, _ = run(WHEEL, {"items": BASE}, extra=["--seed", "20261024", "--top", "3"])
    check("转盘同种子可复现",
          bool(same1.get("picked")) and
          [p["code"] for p in same1.get("picked", [])] == [p["code"] for p in same2.get("picked", [])],
          same1.get("error") or "")

    distinct = set()
    for seed in range(1, 11):
        r, _, _ = run(WHEEL, {"items": BASE}, extra=["--seed", str(seed), "--top", "1"])
        picked = r.get("picked") or []
        if picked:
            distinct.add(picked[0]["code"])
    check("转盘不同种子结果有差异（不是假随机）", len(distinct) >= 2, sorted(distinct))

    # --- 完整任务：一句话需求端到端跑通 ---
    plan, rc, err = run(OPTIMIZER, {"budget": 30, "people": 1, "goal": "rich", "max_items": 3,
                                    "require_categories": ["主食"], "items": BASE})
    plans = plan.get("top_plans") or []
    codes = [{i["code"] for i in p["items"]} for p in plans]
    check("完整任务：30 元 1 人丰富优先能出方案", bool(plans) and rc == 0, plan.get("error") or "无方案")
    check("完整任务：方案不超预算且含主食",
          bool(plans) and all(p["total_price"] <= 30 + 1e-9 for p in plans) and
          all(any(c.startswith("M") for c in cs) for cs in codes),
          [p.get("total_price") for p in plans])

    # --- P6：非食品伪装成食品分类也必须被确定性剔除 ---
    dirty = [dict(i, category="主食") if i["code"] == "N1" else dict(i) for i in BASE]
    hidden, rc, _ = run(OPTIMIZER, {"budget": 60, "people": 1, "max_items": 2,
                                    "require_categories": ["主食"], "items": dirty})
    leaked = sorted({i["code"] for p in (hidden.get("top_plans") or []) for i in p["items"]} & NON_FOOD)
    check("非食品伪装成主食也进不了推荐（P6 确定性规则）",
          not leaked and hidden.get("non_food_excluded") == len(NON_FOOD),
          [i["name"] for i in BASE if i["code"] in leaked] or hidden.get("non_food_excluded"))

    # --- 券后实付排序：菜单价排序不是最终结论 ---
    rank, rc, _ = run(RANK, {
        "goal": "cheap",
        "candidates": [
            {"label": "A 菜单价更低", "items": [{"code": "M2", "quantity": 1}], "estimate": 17.5},
            {"label": "B 券后更低", "items": [{"code": "M1", "quantity": 1}], "estimate": 22.5},
        ],
        "quotes": [
            {"label": "A 菜单价更低", "coupon": None, "pay": 17.5, "note": "未用券"},
            {"label": "B 券后更低", "coupon": None, "pay": 22.5, "note": "未用券"},
            {"label": "B 券后更低", "coupon": "C123", "pay": 12.5, "note": "汉堡券 -10"},
        ],
    })
    check("券后实付排序：用券方案反超排第一",
          rank.get("best", {}).get("label") == "B 券后更低", rank.get("best") or rank.get("error"))
    check("券后实付排序：预估与实付不一致时告警并指明方案",
          len(rank.get("warnings") or []) == 1 and "B 券后更低" in (rank.get("warnings") or [""])[0],
          rank.get("warnings"))

    consistent, _, _ = run(RANK, {
        "goal": "cheap",
        "candidates": [{"label": "A", "items": [{"code": "M2", "quantity": 1}], "estimate": 17.5}],
        "quotes": [{"label": "A", "coupon": None, "pay": 17.5, "note": "未用券"}],
    })
    check("券后实付排序：预估与实付一致时不误报告警",
          not consistent.get("warnings") and consistent.get("ranked", [{}])[0].get("estimate_delta") == 0,
          consistent.get("warnings") or consistent.get("ranked"))

    # --- 忌口两级语义：strict 硬排除，reluctant 只降权不消失 ---
    with tempfile.TemporaryDirectory() as tmp:
        prof = os.path.join(tmp, "mcd-profile.json")
        for cmd in (["add", "--name", "牛肉", "--level", "strict", "--keywords", "牛肉"],
                    ["add", "--name", "辣", "--level", "reluctant"]):
            proc = subprocess.run([sys.executable, str(PROFILE), "--path", prof] + cmd,
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
            if proc.returncode != 0:
                check("忌口档案写入两级忌口", False, proc.stdout or proc.stderr)
                break
        else:
            eff = subprocess.run([sys.executable, str(PROFILE), "--path", prof, "effective"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
            try:
                effective = json.loads(eff.stdout)
            except json.JSONDecodeError:
                effective = {}
            check("忌口档案：两级分类正确（strict/reluctant）",
                  effective.get("strict") == ["牛肉"] and effective.get("reluctant") == ["辣"],
                  effective or eff.stdout)

            strict_plan, _, _ = run(OPTIMIZER, {"budget": 40, "people": 1, "max_items": 2,
                                                "require_categories": ["主食"],
                                                "exclude_keywords": effective.get("strict", []),
                                                "soft_exclude_keywords": effective.get("reluctant", []),
                                                "items": BASE})
            plans = strict_plan.get("top_plans") or []
            beef_hits = sorted({i["code"] for p in plans for i in p["items"] if i["code"] == "M1"})
            check("strict 忌口：含牛肉的巨无霸绝不出现", not beef_hits, beef_hits)

            relaxed_plans = [p for p in plans if p.get("has_reluctant")]
            check("reluctant 忌口：出现即打标",
                  all(p.get("reluctant_matches") == ["辣"] for p in relaxed_plans),
                  [(p.get("has_reluctant"), p.get("reluctant_matches")) for p in plans])
            # 「勉强接受」不消失，但必须排在干净方案之后
            first_reluctant = next((i for i, p in enumerate(plans) if p.get("has_reluctant")), None)
            check("reluctant 忌口：排序靠后而非硬排除",
                  first_reluctant is None or all(p.get("has_reluctant") for p in plans[first_reluctant:]),
                  [p.get("has_reluctant") for p in plans])

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    for n, ok, e in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  → {e}" if not ok and e else ""))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
