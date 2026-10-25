#!/usr/bin/env python3
"""真实菜单快照的端到端测试：验证脚本在 query-meals 真实返回形状下的行为。

用 fixtures/real_menu_1420450.json（2026-10-24 抓取的深圳福田高铁站餐厅菜单），
全程离线不调接口。覆盖离线样例数据里没有、只有真实菜单才有的特征：
混合分类、非食品周边、多人餐高价品、tags 缺失、随心配低价主食。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "real_menu_1420450.json"
OPTIMIZER = HERE / "combo_optimizer.py"
WHEEL = HERE / "spin_wheel.py"

with open(FIXTURE, encoding="utf-8") as f:
    MENU = json.load(f)["items"]

NON_FOOD = {"521956", "521744"}          # 麦当劳限定丝巾 / 薯条脆卜卜毛绒周边
MULTI = {"9900015151", "9900011056", "9900015730"}  # 双人餐与拼盘，单价均 > 40
BY_CODE = {i["code"]: i for i in MENU}


def dirty_menu():
    """还原接口真实形态：把非食品伪装成食品分类（P6 缺陷现场）。

    fixture 里这两项已被人工标成「周边」，而真实 query-meals 会把丝巾放在
    「人气热卖」、毛绒周边放在「小食甜品/其他」。这里故意改回食品分类并清掉
    名称里的敏感词以外的信息，用来验证脚本侧的确定性识别是否真的生效。
    """
    dirty = []
    for it in MENU:
        copy = dict(it)
        if copy["code"] in NON_FOOD:
            copy["category"] = "小食"
        dirty.append(copy)
    return dirty


DIRTY = dirty_menu()


def run(script, payload, extra=None):
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run([sys.executable, str(script), "--input", path] + (extra or []),
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"_raw": proc.stdout, "_err": proc.stderr, "_rc": proc.returncode}
    finally:
        if os.path.exists(path):
            os.remove(path)


def plan_codes(plan):
    return {it.get("code") for it in plan.get("items", [])}


def main():
    results = []

    def check(name, ok, extra=""):
        results.append((name, ok, extra))

    # 1. 真实菜单能跑出结果，且规模不触发崩溃
    r1 = run(OPTIMIZER, {"budget": 35, "people": 1, "goal": "rich", "max_items": 4,
                         "require_categories": ["主食"], "exclude_categories": ["周边"], "items": MENU})
    plans = r1.get("top_plans") or []
    check("真实菜单（60 项）能产出方案", bool(plans), r1.get("error") or r1.get("_err") or "无方案")

    # 2. 每个方案都必须满足硬约束：含主食、不超预算
    bad_budget = [p for p in plans if p.get("total_price", 0) > 35 + 1e-9]
    check("所有方案不超预算 35 元", not bad_budget, [p.get("total_price") for p in bad_budget])
    no_staple = [p for p in plans if not any(
        BY_CODE.get(c, {}).get("category") == "主食" for c in plan_codes(p))]
    check("所有方案都含主食", not no_staple, len(no_staple))

    # 3. 非食品周边不得进入推荐（真实菜单里丝巾 34.9、毛绒周边 24.9 混在餐品中）
    hit = sorted({c for p in plans for c in plan_codes(p)} & NON_FOOD)
    check("非食品周边（丝巾/毛绒周边）不进推荐", not hit,
          [BY_CODE[c]["name"] for c in hit] or "已排除")

    # 4. 低价优先场景
    r2 = run(OPTIMIZER, {"budget": 35, "people": 1, "goal": "cheap", "max_items": 4,
                         "require_categories": ["主食"], "exclude_categories": ["周边"], "items": MENU})
    cheap = r2.get("top_plans") or []
    check("低价优先也有方案且含主食", bool(cheap) and all(
        any(BY_CODE.get(c, {}).get("category") == "主食" for c in plan_codes(p)) for p in cheap),
        r2.get("error") or "")
    if plans and cheap:
        check("低价优先的最低价不高于丰富优先",
              min(p["total_price"] for p in cheap) <= min(p["total_price"] for p in plans) + 1e-9,
              f"cheap={min(p['total_price'] for p in cheap)} rich={min(p['total_price'] for p in plans)}")

    # 5. 忌口过滤在真实菜单上生效（排除牛肉相关）
    r3 = run(OPTIMIZER, {"budget": 35, "people": 1, "goal": "rich", "max_items": 4,
                         "require_categories": ["主食"], "exclude_keywords": ["牛肉", "安格斯", "巨无霸"],
                         "items": MENU})
    beef = sorted({c for p in (r3.get("top_plans") or []) for c in plan_codes(p)
                   if any(k in BY_CODE.get(c, {}).get("name", "") or
                          any(k in t for t in BY_CODE.get(c, {}).get("tags", []))
                          for k in ("牛肉", "安格斯", "巨无霸"))})
    check("忌口过滤：含牛肉/安格斯/巨无霸的餐品被排除", not beef, [BY_CODE[c]["name"] for c in beef])

    # 6. 预算明显低于多人餐时，多人餐不得入选
    r4 = run(OPTIMIZER, {"budget": 30, "people": 1, "goal": "rich", "max_items": 3,
                         "require_categories": ["主食"], "exclude_categories": ["周边"], "items": MENU})
    multi_hit = sorted({c for p in (r4.get("top_plans") or []) for c in plan_codes(p)} & MULTI)
    check("预算 30 时不会选进 40 元以上的多人餐/拼盘", not multi_hit,
          [BY_CODE[c]["name"] for c in multi_hit])

    # 7. 转盘在真实菜单上：tags 缺失时靠名称兜底，且不会抽到周边
    r5 = run(WHEEL, {"budget": 30, "mode": "any", "exclude_categories": ["周边"], "items": MENU}, extra=["--seed", "7", "--top", "5"])
    picks = r5.get("picked") or []
    check("转盘在真实菜单能抽出结果", bool(picks), r5.get("error") or "空池")
    wheel_hit = sorted({p.get("code") for p in picks} & NON_FOOD)
    check("转盘不会抽到非食品周边", not wheel_hit, [BY_CODE[c]["name"] for c in wheel_hit])
    over = [p for p in picks if p.get("price", 0) > 30 + 1e-9]
    check("转盘结果不超预算 30", not over, [p.get("name") for p in over])

    # 8. 只抽套餐时，tags 缺失的「随心配」「乐园餐」靠名称识别
    r6 = run(WHEEL, {"budget": 30, "mode": "combo", "exclude_categories": ["周边"], "items": MENU}, extra=["--seed", "7"])
    combo_pick = r6.get("picked") or []
    check("mode=combo 抽中的确实都是套餐类",
          bool(combo_pick) and all(
              "套餐" in (p.get("tags") or []) or any(
                  k in p.get("name", "") for k in ("件套", "随心配", "乐园餐", "套餐"))
              for p in combo_pick),
          [p.get("name") for p in combo_pick if not (
              "套餐" in (p.get("tags") or []) or any(
                  k in p.get("name", "") for k in ("件套", "随心配", "乐园餐", "套餐")))])


    # 9. P6 回归：菜单把周边伪装成食品分类且**不传** exclude_categories，也必须被剔除
    r7 = run(OPTIMIZER, {"budget": 60, "people": 1, "goal": "rich", "max_items": 2,
                         "require_categories": ["主食"], "items": DIRTY})
    dirty = sorted({c for p in (r7.get("top_plans") or []) for c in plan_codes(p)} & NON_FOOD)
    check("周边伪装成小食且不传 exclude_categories 时仍被确定性剔除", not dirty,
          [BY_CODE[c]["name"] for c in dirty])
    check("伪装菜单上 non_food_excluded 报告为 2", r7.get("non_food_excluded") == len(NON_FOOD),
          r7.get("non_food_excluded"))

    r8 = run(OPTIMIZER, {"budget": 60, "people": 1, "goal": "rich", "max_items": 2,
                         "require_categories": ["主食"], "exclude_categories": ["周边"], "items": MENU})
    clean = sorted({c for p in (r8.get("top_plans") or []) for c in plan_codes(p)} & NON_FOOD)
    check("传入 exclude_categories 后高预算场景不再混入周边", not clean,
          [BY_CODE[c]["name"] for c in clean])

    # 10. 转盘多种子扫描：整类排除后所有种子都不该抽到周边
    wheel_hits = []
    for seed in range(1, 11):
        r = run(WHEEL, {"mode": "any", "exclude_categories": ["周边"], "items": MENU},
                extra=["--seed", str(seed), "--top", "3"])
        wheel_hits += [p.get("code") for p in (r.get("picked") or []) if p.get("code") in NON_FOOD]
    check("转盘 10 个种子 × 3 抽全部避开周边", not wheel_hits,
          [BY_CODE[c]["name"] for c in wheel_hits])

    # 11. P6 回归：转盘在伪装菜单上不传 exclude_categories，池子必须少 2 项且抽不到周边
    r9 = run(WHEEL, {"mode": "any", "items": DIRTY}, extra=["--seed", "3"])
    check("转盘在伪装菜单上剔除全部非食品",
          r9.get("non_food_excluded") == len(NON_FOOD) and
          (r9.get("pool_size") or 0) == len(MENU) - len(NON_FOOD),
          f"pool={r9.get('pool_size')} excluded={r9.get('non_food_excluded')}")
    dirty_wheel = []
    for seed in range(1, 11):
        r = run(WHEEL, {"mode": "any", "items": DIRTY}, extra=["--seed", str(seed), "--top", "3"])
        dirty_wheel += [p.get("code") for p in (r.get("picked") or []) if p.get("code") in NON_FOOD]
    check("伪装菜单上转盘 10 种子 × 3 抽也抽不到周边", not dirty_wheel,
          [BY_CODE[c]["name"] for c in dirty_wheel])


    # 12. 热量上限：营养库只能核实到单品，套餐类查不到热量，设上限后必须被排除
    verified = [i for i in MENU if i.get("kcal") is not None]
    check("营养匹配率极低（真实情况）：可核实餐品占少数",
          0 < len(verified) < len(MENU) / 3, f"{len(verified)}/{len(MENU)}")

    r10 = run(OPTIMIZER, {"budget": 35, "people": 1, "goal": "rich", "max_items": 3,
                          "require_categories": ["主食"], "exclude_categories": ["周边"],
                          "max_kcal": 600, "items": MENU})
    kcal_plans = r10.get("top_plans") or []
    check("设 max_kcal=600 后仍有可行方案", bool(kcal_plans), r10.get("error") or "无方案")
    over_kcal = [p for p in kcal_plans if (p.get("total_kcal") or 0) > 600]
    check("方案热量都不超 600", not over_kcal, [p.get("total_kcal") for p in over_kcal])
    unverified = [p for p in kcal_plans if not p.get("kcal_verified")]
    check("设上限时不允许出现未核实热量的方案", not unverified,
          [i["name"] for p in unverified for i in p["items"] if i.get("kcal") is None])
    combo_names = [i["name"] for p in kcal_plans for i in p["items"]
                   if any(k in i["name"] for k in ("件套", "套餐", "随心配"))]
    check("套餐类因热量不可核实而无法进入设限方案", not combo_names, combo_names)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    for n, ok, e in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  → {e}" if not ok and e else ""))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
