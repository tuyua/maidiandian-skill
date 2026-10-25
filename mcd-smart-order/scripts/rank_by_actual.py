#!/usr/bin/env python3
"""券后实付排序：把脚本候选方案与真实算价结果合并，按实付金额决出最优惠的一单。

脚本（combo_optimizer）只能算菜单预估价，不含券、满减、随单购、运费；
真正的「最优惠」必须由 calculate-price 的实付结果决定。本脚本负责这最后一步排序。

用法：
    python rank_by_actual.py --input rank_input.json
    cat rank_input.json | python rank_by_actual.py

输入 JSON（金额单位：元）：
{
  "goal": "cheap",
  "candidates": [
    {"label": "方案A", "items": [{"code": "9900013304", "quantity": 1}], "estimate": 14.9}
  ],
  "quotes": [
    {"label": "方案A", "coupon": null, "pay": 14.9, "note": "未用券"},
    {"label": "方案A", "coupon": "C123", "pay": 11.9, "note": "麦辣鸡腿堡券"}
  ]
}

quotes[].coupon 为 null 或省略表示该次报价未使用优惠券；同一个 label 可有多条报价。

输出：按每个方案的**最优实付**升序排列，并给出券前券后差额与预估偏差。
"""

import argparse
import json
import sys

from mcd_common import InputError, to_cents


def validate(data):
    if not isinstance(data, dict):
        raise InputError("输入必须是 JSON 对象")

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise InputError("candidates 必须是非空数组")

    labels = []
    for idx, item in enumerate(candidates):
        if not isinstance(item, dict):
            raise InputError(f"candidates[{idx}] 必须是对象")
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise InputError(f"candidates[{idx}].label 必须是非空字符串")
        if label in labels:
            raise InputError(f"重复的候选标签：{label}")
        labels.append(label)

    quotes = data.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        raise InputError("quotes 必须是非空数组，至少要有未用券的基准报价")

    parsed = []
    for idx, quote in enumerate(quotes):
        if not isinstance(quote, dict):
            raise InputError(f"quotes[{idx}] 必须是对象")
        label = quote.get("label")
        if label not in labels:
            raise InputError(f"quotes[{idx}].label 不在候选列表中：{label!r}")
        if quote.get("pay") is None:
            raise InputError(f"quotes[{idx}].pay 不能为空")
        pay = to_cents(quote.get("pay"))
        parsed.append({
            "label": label,
            "coupon": quote.get("coupon"),
            "pay_cents": pay,
            "pay": pay / 100,
            "note": quote.get("note", ""),
        })

    return candidates, parsed


def main():
    parser = argparse.ArgumentParser(description="按真实算价结果排序，决出最优惠方案")
    parser.add_argument("--input", help="输入 JSON 文件路径，缺省从 stdin 读取")
    args = parser.parse_args()

    def fail(message):
        print(json.dumps({"error": message}, ensure_ascii=False, indent=2))
        sys.exit(1)

    try:
        if args.input:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)
    except OSError as e:
        fail(f"输入文件读取失败：{e}")
        return
    except json.JSONDecodeError as e:
        fail(f"输入不是合法 JSON：{e}")
        return

    try:
        candidates, quotes = validate(data)
    except InputError as e:
        fail(str(e))
        return

    ranked = []
    warnings = []

    for cand in candidates:
        label = cand["label"]
        mine = [q for q in quotes if q["label"] == label]
        base = [q for q in mine if not q["coupon"]]
        if not base:
            warnings.append(f"{label} 缺少未用券的基准报价，无法计算券后节省额")
            base_pay = None
        else:
            base_pay = min(q["pay_cents"] for q in base)

        best = min(mine, key=lambda q: q["pay_cents"])
        estimate = cand.get("estimate")
        estimate_cents = to_cents(estimate) if estimate is not None else None

        delta = None
        if estimate_cents is not None:
            delta = (best["pay_cents"] - estimate_cents) / 100
            if abs(delta) >= 0.01:
                warnings.append(
                    f"{label} 实付与菜单预估相差 {delta:+.2f} 元，以实付为准"
                    + ("（出现了预估未包含的优惠）" if delta < 0 else "（可能有预估未计入的费用）")
                )

        ranked.append({
            "label": label,
            "items": cand.get("items", []),
            "best_pay": best["pay"],
            "best_coupon": best["coupon"],
            "best_note": best["note"],
            "base_pay": None if base_pay is None else base_pay / 100,
            "saved_by_coupon": None if base_pay is None else (base_pay - best["pay_cents"]) / 100,
            "estimate": estimate,
            "estimate_delta": delta,
        })

    ranked.sort(key=lambda r: r["best_pay"])
    best = ranked[0]

    print(json.dumps(
        {
            "goal": data.get("goal", "cheap"),
            "best": {"label": best["label"], "pay": best["best_pay"],
                     "coupon": best["best_coupon"], "note": best["best_note"]},
            "ranked": ranked,
            "warnings": warnings,
            "price_basis": "calculated_actual",
            "note": "排序依据是 calculate-price 的实付金额，不是菜单预估价",
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
