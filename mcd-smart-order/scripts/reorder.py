#!/usr/bin/env python3
"""历史订单一键复购：把上一单的明细，对照当前门店菜单，找出「还能买 / 已下架 / 涨价了」。

设计意图：order-list 只能拿到历史订单的快照（code/name/当时价格），能不能原样再下一单，
取决于这些 code 在当前门店 query-meals 里是否还在售。本脚本做对照，并直接产出：
    - reorder_payload：仅含「仍在售」的条目与原始数量，可直接喂 calculate-price 重新算价
    - available / discontinued：带差价与状态，供 Agent 向用户解释「为什么不能直接复购」

纯离线、不产生订单。真实使用时把 order-list 结果塞进 past_order、把 query-meals 结果
塞进 current_menu 即可。

用法：
    python reorder.py --input reorder_input.json
    cat reorder_input.json | python reorder.py
"""

import argparse
import json
import sys

from mcd_common import InputError, to_cents


def _norm(s):
    return (s or "").strip()


def _index_menu(menu):
    """建立 code→item 与 name→item 两份索引（名称索引用于下架项的近似匹配）。"""
    by_code, by_name = {}, {}
    for item in menu:
        if not isinstance(item, dict):
            continue
        code = _norm(item.get("code"))
        name = _norm(item.get("name"))
        if code:
            by_code.setdefault(code, item)
        if name:
            by_name.setdefault(name, item)
    return by_code, by_name


def _resolve_current(past_item, by_code, by_name):
    """在历史订单条目与现菜单之间定位当前在售项，返回 (cur_item, match_method)。

    match_method:
        "code"         —— 编码精确命中（最可靠）
        "name_exact"   —— 名称精确命中
        "name_approx"  —— 名称子串命中（产品改名/换字眼，需向用户确认）
        None           —— 现菜单里找不到，已下架
    """
    code = _norm(past_item.get("code"))
    name = _norm(past_item.get("name"))
    if code and code in by_code:
        return by_code[code], "code"
    if name and name in by_name:
        return by_name[name], "name_exact"
    # 子串匹配：历史名被改了字眼（如「麦辣鸡腿堡（旧名）」vs「麦辣鸡腿堡」）
    if name:
        for cur_name, alt in by_name.items():
            if name in cur_name or cur_name in name:
                return alt, "name_approx"
    return None, None


def _suggest_replacement(past_item, by_name):
    """下架项按名称在现菜单里找近似替代，返回 (matched_name, matched_price) 或 None。"""
    name = _norm(past_item.get("name"))
    if not name:
        return None
    if name in by_name:
        alt = by_name[name]
        return _norm(alt.get("name")), _safe_price(alt.get("price"))
    for cur_name, alt in by_name.items():
        if name in cur_name or cur_name in name:
            return cur_name, _safe_price(alt.get("price"))
    return None


def _safe_price(v):
    try:
        return round(to_cents(v) / 100, 2)
    except InputError:
        return None


def main():
    parser = argparse.ArgumentParser(description="麦当劳历史订单复购对照")
    parser.add_argument("--input", help="输入 JSON 文件路径，缺省从 stdin 读取")
    args = parser.parse_args()

    def fail(message, code):
        print(json.dumps({"error": message, "error_code": code}, ensure_ascii=False, indent=2))
        sys.exit(1)

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

    past = payload.get("past_order")
    if not isinstance(past, list) or not past:
        fail("past_order 必须是非空数组（来自 order-list 的明细）", "invalid_param")
        return

    menu = payload.get("current_menu") or []
    if not isinstance(menu, list):
        fail("current_menu 必须是数组（来自 query-meals）", "invalid_param")
        return

    by_code, by_name = _index_menu(menu)

    available, discontinued = [], []
    reorder_items = []
    total_now = 0.0

    for line in past:
        if not isinstance(line, dict):
            continue
        code = _norm(line.get("code"))
        name = _norm(line.get("name"))
        qty = line.get("quantity", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            qty = 1
        then_price = _safe_price(line.get("price"))

        cur, method = _resolve_current(line, by_code, by_name)
        if cur is None:
            # 下架：尝试按名称近似找替代
            alt = _suggest_replacement(line, by_name)
            discontinued.append({
                "code": code or None,
                "name": name,
                "then_price": then_price,
                "suggested_replacement": ({"name": alt[0], "price": alt[1]} if alt else None),
            })
            continue

        now_price = _safe_price(cur.get("price"))
        if then_price is not None and now_price is not None:
            delta = round(now_price - then_price, 2)
            if delta > 0:
                status = "price_up"
            elif delta < 0:
                status = "price_down"
            else:
                status = "unchanged"
        else:
            delta = None
            status = "unknown_price"

        available.append({
            "code": _norm(cur.get("code")) or code,
            "name": _norm(cur.get("name")) or name,
            "then_price": then_price,
            "now_price": now_price,
            "delta": delta,
            "status": status,
            "quantity": qty,
            "matched_by": method,
            "name_changed": method == "name_approx",
        })
        if now_price is not None:
            total_now += now_price * qty
        reorder_items.append({
            "code": _norm(cur.get("code")) or code,
            "name": _norm(cur.get("name")) or name,
            "price": now_price,
            "quantity": qty,
        })

    total_now = round(total_now, 2)
    preview_budget = round(total_now * 1.1 + 0.5)  # 给 10% 余量并取整，方便直接当预算用

    summary_parts = [
        f"在售 {len(available)} 项、已下架 {len(discontinued)} 项",
        f"原样复购现价合计 ¥{total_now:.2f}",
    ]
    ups = [a for a in available if a["status"] == "price_up"]
    if ups:
        summary_parts.append(f"其中 {len(ups)} 项涨价")
    summary = "；".join(summary_parts) + "。"

    output = {
        "price_basis": "menu_estimate",
        "available": available,
        "discontinued": discontinued,
        "reorder_payload": {
            "items": reorder_items,
            "total_now": total_now,
            "preview_budget": preview_budget,
        },
        "summary": summary,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
