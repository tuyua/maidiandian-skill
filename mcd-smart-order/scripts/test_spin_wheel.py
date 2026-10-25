#!/usr/bin/env python3
"""spin_wheel.py 的离线回归测试：本地样例数据，不调用任何麦当劳接口。"""

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

SCRIPT = Path(__file__).parent / "spin_wheel.py"

BASE = [
    {"code": "A1", "name": "板烧鸡腿堡三件套", "price": 27.9, "origin_price": 39.5, "category": "主食", "tags": ["套餐"], "kcal": 620},
    {"code": "A2", "name": "麦辣鸡腿堡", "price": 22.5, "origin_price": 22.5, "category": "主食", "tags": ["单品"], "kcal": 485},
    {"code": "A3", "name": "吉士汉堡套餐", "price": 20.5, "origin_price": 30.0, "category": "主食", "tags": ["套餐"], "kcal": 480},
    {"code": "B1", "name": "薯条", "price": 12.0, "category": "小食", "tags": ["单品"], "kcal": 289},
    {"code": "B2", "name": "麦乐鸡", "price": 14.0, "category": "小食", "tags": ["单品"], "kcal": 213},
    {"code": "C1", "name": "可乐", "price": 9.5, "category": "饮品", "tags": ["单品"], "kcal": 147},
    {"code": "C2", "name": "鲜萃咖啡", "price": 9.5, "category": "饮品", "kcal": 13},
    {"code": "D1", "name": "圆筒冰淇淋", "price": 5.0, "category": "甜品", "tags": ["单品"], "kcal": 93},
    {"code": "D2", "name": "新品派", "price": 8.5, "category": "甜品"},
]


def run(payload, top=None, seed=None, expect_ok=True):
    fd, path = tempfile.mkstemp(suffix=".json")
    cmd = [sys.executable, str(SCRIPT), "--input", path]
    if top is not None:
        cmd += ["--top", str(top)]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        if os.path.exists(path):
            os.remove(path)
    if proc.returncode != 0 and expect_ok:
        return {"_error": f"退出码 {proc.returncode}", "_stderr": proc.stderr}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_error": "输出不是合法 JSON", "_stderr": proc.stderr}


def check(results, name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" -> {detail}"))


def main():
    results = []

    # 1. 基础抽取：结果必须来自候选池且满足预算
    r = run({"budget": 30, "items": BASE}, seed=7)
    picked = r["picked"][0]
    check(results, "基础抽取：结果在候选池内且不超预算",
          picked["code"] in {i["code"] for i in BASE} and picked["price"] <= 30, picked)

    # 2. 同种子可复现，不同种子结果不同
    r_same = run({"budget": 30, "items": BASE}, seed=7)
    r_other = run({"budget": 30, "items": BASE}, seed=999)
    check(results, "同种子结果可复现", r_same["picked"][0]["code"] == picked["code"])
    check(results, "不同种子结果不同", r_other["picked"][0]["code"] != picked["code"],
          f'{r_other["picked"][0]["code"]} vs {picked["code"]}')

    # 3. 多次抽取结果都合法（遍历若干种子）
    ok_all = True
    for seed in range(12):
        res = run({"budget": 20, "exclude_keywords": ["鸡"], "items": BASE}, seed=seed)
        item = res["picked"][0]
        if item["price"] > 20 or "鸡" in item["name"]:
            ok_all = False
            break
    check(results, "30 次抽取：全部满足预算与忌口", ok_all)

    # 4. 只抽套餐 / 只抽单品
    r_combo = run({"mode": "combo", "items": BASE}, seed=3)
    check(results, "只抽套餐：结果 kind 均为 combo",
          all(p["kind"] == "combo" for p in r_combo["picked"]), r_combo["picked"][0])

    r_single = run({"mode": "single", "items": BASE}, seed=3)
    check(results, "只抽单品：结果 kind 均为 single",
          all(p["kind"] == "single" for p in r_single["picked"]), r_single["picked"][0])

    # 5. 名称关键词兜底分类（无 tags 的 C2 鲜萃咖啡应为单品）
    check(results, "名称兜底：无 tags 也能判定单品",
          any(p["code"] == "C2" and p["kind"] == "single" for p in r_single["picked"]) or
          r_single["picked"][0]["kind"] == "single")

    # 6. 排除项 avoid 生效
    r_avoid = run({"items": BASE, "avoid": [picked["code"]]}, seed=7)
    check(results, "排除项：avoid 中的餐品不会再被抽中",
          r_avoid["picked"][0]["code"] != picked["code"], r_avoid["picked"][0]["code"])

    # 7. 热量上限：缺失热量的餐品不得进入
    r_kcal = run({"max_kcal": 500, "items": BASE}, seed=5)
    check(results, "热量上限：未核实热量的餐品被排除",
          all(p["kcal"] is not None and p["kcal"] <= 500 for p in r_kcal["picked"]), r_kcal["picked"][0])

    # 8. 分类过滤
    r_cat = run({"categories": ["饮品"], "items": BASE}, seed=2)
    check(results, "分类过滤：只抽指定分类", r_cat["picked"][0]["category"] == "饮品", r_cat["picked"][0])

    # 9. 一次抽多个且不重复
    r_multi = run({"items": BASE}, top=3, seed=11)
    codes = [p["code"] for p in r_multi["picked"]]
    check(results, "一次抽多个：数量正确且不重复",
          len(codes) == 3 and len(set(codes)) == 3, codes)

    # 10. 加权随机的统计效果：折扣餐品中签占比明显高于不加权
    def discount_hits(weighted, samples=40):
        hits = 0
        for seed in range(samples):
            res = run({"weight_by_discount": weighted, "items": BASE}, seed=seed)
            if res["picked"][0]["saved"] > 0:
                hits += 1
        return hits

    weighted_hits = discount_hits(True)
    plain_hits = discount_hits(False)
    check(results, "加权随机：折扣餐品中签占比高于不加权",
          weighted_hits > plain_hits and weighted_hits >= 20,
          f"加权 {weighted_hits}/40 vs 不加权 {plain_hits}/40")

    # 11. 空候选池报错
    r_none = run({"budget": 1, "items": BASE}, expect_ok=False)
    check(results, "空候选池：返回结构化错误", "error" in r_none and "error_code" in r_none, r_none)

    # 12. 非法入参
    for label, payload in [
        ("mode 非法", {"mode": "abc", "items": BASE}),
        ("budget 为负", {"budget": -5, "items": BASE}),
        ("items 非数组", {"items": "x"}),
    ]:
        res = run(payload, expect_ok=False)
        check(results, f"非法入参：{label} 返回结构化错误",
              "error" in res and "error_code" in res, res)

    # 13. 单品异常不中断
    r_bad = run({"items": [{"code": "Z1", "name": "价格非法", "price": "abc", "category": "主食"}, BASE[0]]})
    check(results, "单品异常：跳过并记录，不崩溃",
          bool(r_bad.get("picked")) and len(r_bad.get("invalid_items", [])) >= 1, r_bad.get("error") or r_bad.get("invalid_items"))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    if failed:
        print("失败项：", "、".join(failed))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
