#!/usr/bin/env python3
"""rank_by_actual.py 的离线测试：全部本地数据，不调用任何麦当劳接口。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "rank_by_actual.py"

CANDIDATES = [
    {"label": "方案A", "items": [{"code": "A1", "quantity": 1}], "estimate": 29.8},
    {"label": "方案B", "items": [{"code": "A2", "quantity": 2}], "estimate": 27.9},
    {"label": "方案C", "items": [{"code": "A3", "quantity": 1}], "estimate": 24.0},
]


def run(payload):
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--input", path],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        if os.path.exists(path):
            os.remove(path)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_error": proc.stdout or proc.stderr}


def check(results, name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" -> {detail}"))


def main():
    results = []

    # 1. 基本排序：按最优实付升序
    r = run({
        "candidates": CANDIDATES,
        "quotes": [
            {"label": "方案A", "pay": 29.8},
            {"label": "方案B", "pay": 27.9},
            {"label": "方案C", "pay": 24.0},
        ],
    })
    order = [x["label"] for x in r["ranked"]]
    check(results, "基本排序：按实付升序", order == ["方案C", "方案B", "方案A"], order)
    check(results, "最优方案正确", r["best"]["label"] == "方案C", r["best"])

    # 2. 用券后反超：A 用券后实付最低，应排第一
    r2 = run({
        "candidates": CANDIDATES,
        "quotes": [
            {"label": "方案A", "pay": 29.8},
            {"label": "方案A", "coupon": "C1", "pay": 19.8, "note": "满减券"},
            {"label": "方案B", "pay": 27.9},
            {"label": "方案C", "pay": 24.0},
        ],
    })
    check(results, "用券反超：券后实付最低者排第一",
          r2["best"]["label"] == "方案A" and r2["best"]["pay"] == 19.8, r2["best"])
    check(results, "券后节省额计算正确",
          r2["ranked"][0]["saved_by_coupon"] == 10.0, r2["ranked"][0]["saved_by_coupon"])

    # 3. 实付与预估不一致时给出警告并以实付为准
    r3 = run({
        "candidates": CANDIDATES,
        "quotes": [
            {"label": "方案A", "pay": 25.0},
            {"label": "方案B", "pay": 27.9},
            {"label": "方案C", "pay": 24.0},
        ],
    })
    check(results, "预估偏差：给出警告",
          any("实付与菜单预估相差" in w for w in r3["warnings"]), r3["warnings"])
    check(results, "预估偏差：排序仍按实付",
          r3["ranked"][0]["pay"] if False else r3["best"]["pay"] == 24.0, r3["best"])

    # 4. 缺少基准报价时提示
    r4 = run({
        "candidates": [CANDIDATES[0]],
        "quotes": [{"label": "方案A", "coupon": "C1", "pay": 19.8}],
    })
    check(results, "缺少未用券基准：给出提示且节省额为空",
          any("缺少未用券的基准报价" in w for w in r4["warnings"])
          and r4["ranked"][0]["saved_by_coupon"] is None, r4)

    # 5. 标签对不上应报错
    r5 = run({"candidates": CANDIDATES, "quotes": [{"label": "方案Z", "pay": 10}]})
    check(results, "报价标签不存在：报错", "error" in r5, r5)

    # 6. 非法入参
    for label, payload in [
        ("candidates 为空", {"candidates": [], "quotes": [{"label": "A", "pay": 1}]}),
        ("quotes 为空", {"candidates": CANDIDATES, "quotes": []}),
        ("pay 缺失", {"candidates": CANDIDATES, "quotes": [{"label": "方案A"}]}),
        ("pay 为负", {"candidates": CANDIDATES, "quotes": [{"label": "方案A", "pay": -1}]}),
        ("label 重复", {"candidates": [CANDIDATES[0], CANDIDATES[0]],
                        "quotes": [{"label": "方案A", "pay": 1}]}),
    ]:
        res = run(payload)
        check(results, f"非法入参：{label} 报错", "error" in res, res)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    if failed:
        print("失败项：", "、".join(failed))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
