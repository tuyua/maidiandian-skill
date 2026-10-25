#!/usr/bin/env python3
"""预算不足「差多少」诊断的离线回归：无解时必须给出缺口与最接近的可行组合。

以前的失败路径只有一句「没有满足约束的组合」，用户只能自己猜差在哪。
现在要求：定位到具体约束（缺钱 / 热量冲突 / 分类凑不齐），给出 nearest_plan
与还差多少钱，并且不得把诊断结果包装成推荐。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "combo_optimizer.py"

MENU = [
    {"code": "M1", "name": "麦香鸡", "price": 17.5, "category": "主食", "kcal": 400},
    {"code": "M2", "name": "麦香鱼", "price": 23.0, "category": "主食", "kcal": 420},
    {"code": "S1", "name": "薯条", "price": 12.0, "category": "小食", "kcal": 289},
    {"code": "S2", "name": "脆薯饼", "price": 8.0, "category": "小食", "kcal": 230},
    {"code": "D1", "name": "圆筒冰淇淋", "price": 5.0, "category": "甜品", "kcal": 93},
]

KNOWN_CODES = ("budget_shortage", "kcal_conflict", "category_conflict", "no_plan", "no_candidate")


def run(payload):
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--input", path],
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

    # 1. 预算不够买主食：必须报缺多少钱
    res, rc, err = run({"budget": 10, "require_categories": ["主食"], "items": MENU})
    check("预算不足：错误码为 budget_shortage",
          res.get("error_code") == "budget_shortage" and rc == 1 and "Traceback" not in err,
          f"rc={rc} code={res.get('error_code')}")
    check("预算不足：shortfall 正确（17.5 - 10 = 7.5）",
          res.get("shortfall") == 7.5, res.get("shortfall"))

    nearest = res.get("nearest_plan") or {}
    check("预算不足：给出最接近的可行方案且超过预算",
          nearest.get("total_price") == 17.5 and nearest.get("total_price", 0) > 10,
          nearest.get("total_price"))
    codes = {i["code"] for i in nearest.get("items", [])}
    check("预算不足：最接近方案确实含主食（遵守硬约束）",
          codes & {"M1", "M2"}, codes)

    # 2. 诊断结果不得伪装成推荐
    check("诊断输出不含 top_plans（避免被当成推荐）",
          "top_plans" not in res, list(res.keys()))
    check("诊断输出标明数据来源仍是菜单预估价",
          res.get("price_basis") == "menu_estimate", res.get("price_basis"))
    check("诊断写明临时放宽了哪些约束",
          res.get("relaxed") == ["budget"], res.get("relaxed"))

    # 3. 人数硬约束下缺钱：补齐份数后的差额
    res, _, _ = run({"budget": 20, "people": 2, "require_categories": ["主食"], "items": MENU})
    nearest = res.get("nearest_plan") or {}
    qty = sum(i.get("quantity", 0) for i in nearest.get("items", []))
    check("多人缺钱：最接近方案满足人数份数（2 × 麦香鸡 = 35）",
          res.get("error_code") == "budget_shortage" and qty >= 2 and nearest.get("total_price") == 35.0,
          f"qty={qty} price={nearest.get('total_price')}")
    check("多人缺钱：差额计算正确（35 - 20 = 15）",
          res.get("shortfall") == 15.0, res.get("shortfall"))

    # 4. 热量与预算冲突：必须指名是热量卡住
    res, _, _ = run({"budget": 30, "max_kcal": 200, "require_categories": ["主食"], "items": MENU})
    check("热量冲突：单独识别为 kcal_conflict",
          res.get("error_code") == "kcal_conflict" and "max_kcal" in (res.get("relaxed") or []),
          res.get("error_code"))
    check("热量冲突：错误信息点明热量",
          "热量" in (res.get("error") or ""), res.get("error"))

    # 5. 分类要求凑不齐：指名是分类映射问题
    res, _, _ = run({"budget": 60, "require_categories": ["主食", "汤"], "items": MENU})
    check("分类冲突：识别为 category_conflict 并说明改怎么修",
          res.get("error_code") == "category_conflict" and "主食" in (res.get("error") or ""),
          res.get("error_code"))

    # 6. 全部无解时错误码仍落在已知集合内，不得崩溃
    res, rc, err = run({"budget": 1, "max_kcal": 1, "require_categories": ["主食"], "items": MENU})
    check("极端约束：仍返回已知错误码且不崩溃",
          res.get("error_code") in KNOWN_CODES and "Traceback" not in err,
          f"code={res.get('error_code')}")
    check("预算买不起任何餐品：点名最便宜的一项（圆筒 5 元）",
          res.get("error_code") == "no_candidate" and "圆筒冰淇淋" in (res.get("error") or ""),
          res.get("error"))

    # 7. 有解时完全不受诊断逻辑影响
    ok_res, ok_rc, _ = run({"budget": 30, "require_categories": ["主食"], "items": MENU})
    check("有解场景：返回正常的 top_plans，退出码 0",
          ok_rc == 0 and bool(ok_res.get("top_plans")) and "error" not in ok_res,
          f"rc={ok_rc} keys={list(ok_res)[:6]}")

    # 8. 入参校验优先级不变：重复编码仍是 invalid_item
    dup = MENU + [dict(MENU[0])]
    res, rc, _ = run({"budget": 30, "items": dup})
    check("重复编码：仍是 invalid_item，未被诊断逻辑抢先处理",
          res.get("error_code") == "invalid_item" and rc == 1, res.get("error_code"))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    for n, ok, e in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  → {e}" if not ok and e else ""))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
