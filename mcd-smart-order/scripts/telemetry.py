#!/usr/bin/env python3
"""麦当劳推荐基线记录（脱敏埋点，P5）。

按 diff-internal-test-prompts.md 的 P5 要求，在每次推荐/解释结束后本地脱敏记录基线数据，
支撑 4.5 的 推荐采纳率 / 二次修改率 / 时延基线，并在 M3 复盘时汇总。

设计要点（安全相关）：
  - 仅写入本地文件，绝不上传；明文手机号/地址/姓名在落盘前被强制脱敏。
  - 默认落点 ~/.workbuddy/mcd-telemetry.jsonl（可用 --path 覆盖，测试指向临时文件）。
  - 每行一条 JSON（JSONL），便于流式追加与回放，不读取也不修改用户其他文件。
  - 本脚本零调用任何麦当劳 MCP 接口，也不产生订单，纯本地记录。

子命令：
    python telemetry.py record  --input event.json [--path FILE] [--session-id S]
    python telemetry.py record  --json '{...}'       [--path FILE] [--session-id S]
    python telemetry.py aggregate [--path FILE] [--since ISO]

event.json 字段（均可选，缺省取安全默认值）：
    session_id   会话标识（同一会话内 rec_id 自增）；命令行 --session-id 优先
    constraints  约束对象摘要 dict（会被脱敏：姓名/手机号/完整地址一律剔除或打码）
    recommendation  {"plan_count": int, "default_label": str, "pay_cents": int|null}
    adopted      true / false / "pending"（未决）；非法值报错
    modification 二次修改的结构化记录（list 或 dict），会被脱敏
    turns        int  从首问到给出结果的询问轮次
    latency_ms   int  首问 → 最终可下单方案的时延
    seed         int|null  含转盘/随机时记录，便于复算
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcd-telemetry.jsonl")

# 这些字段名（精确匹配，小写）视为敏感，落盘前脱敏；避免误伤 store_name 等非 PII
_SENSITIVE_EXACT = {"phone", "mobile", "tel", "telephone", "address", "full_address",
                    "addr", "name", "username", "real_name", "user_name", "fullname",
                    "nickname", "token", "openid", "unionid", "id_card", "idcard",
                    "身份证号", "身份证"}
# 这些后缀（精确结尾匹配）也视为敏感，如 home_address / contact_phone
_SENSITIVE_SUFFIX = ("_phone", "_mobile", "_tel", "_address", "_addr")
# 11 位手机号（含常见分隔）的正则，用于在字符串里兜底打码
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d)[-\s]?(\d{4})[-\s]?(\d{4})(?!\d)")


class TelemetryError(Exception):
    """入参不合法或写入失败。调用方必须把错误告知用户，不得静默吞掉。"""


def _mask_phone(text):
    """把字符串里的手机号中间四位打码：13800138000 -> 138****8000。"""
    return _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(3)}", text)


def _is_sensitive_key(key):
    k = str(key).lower()
    if k in _SENSITIVE_EXACT:
        return True
    return any(k.endswith(suffix) for suffix in _SENSITIVE_SUFFIX)


def desensitize(obj):
    """递归脱敏：敏感字段剔除/打码，其余字符串兜底扫描手机号。

    返回脱敏后的副本，不修改入参。脱敏策略：
      - 命中敏感键的字典值：若是电话相关则打码，其余（姓名/地址/令牌）直接置为 "[已脱敏]"
      - 字符串：扫描并打码其中嵌入的手机号
      - 列表/字典：逐元素递归
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if _is_sensitive_key(key):
                if isinstance(value, str) and _PHONE_RE.search(value):
                    out[key] = _mask_phone(value)
                else:
                    out[key] = "[已脱敏]"
            else:
                out[key] = desensitize(value)
        return out
    if isinstance(obj, list):
        return [desensitize(item) for item in obj]
    if isinstance(obj, str):
        return _mask_phone(obj)
    return obj


def _normalize_adopted(value):
    if value is None:
        return "pending"
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("pending", "未决", "未决定", "待定"):
            return "pending"
        if low in ("true", "yes", "采纳", "已采纳", "采纳了"):
            return True
        if low in ("false", "no", "未采纳", "拒绝"):
            return False
        raise TelemetryError(f"adopted 仅支持 true/false/pending：{value!r}")
    raise TelemetryError(f"adopted 类型不支持：{value!r}")


def _next_rec_id(path, session_id):
    """同一 session_id 内自增：统计文件中同会话已有记录数 + 1。"""
    if not os.path.exists(path):
        return 1
    n = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("session_id") == session_id:
                n += 1
    return n + 1


def record_event(payload, path=DEFAULT_PATH, session_id=None):
    """写入一条脱敏基线记录（JSONL），返回写入后的完整记录 dict。

    payload 为 dict；session_id 参数优先于 payload 内的 session_id。
    """
    if not isinstance(payload, dict):
        raise TelemetryError("record 的入参必须是 JSON 对象")

    sid = session_id or payload.get("session_id") or "default"

    constraints = payload.get("constraints")
    recommendation = payload.get("recommendation") or {}
    modification = payload.get("modification")

    rec = {
        "rec_id": _next_rec_id(path, sid),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": sid,
        "constraints": desensitize(constraints) if constraints is not None else None,
        "recommendation": {
            "plan_count": int(recommendation.get("plan_count", 0) or 0),
            "default_label": str(recommendation.get("default_label") or ""),
            "pay_cents": (int(recommendation["pay_cents"])
                          if recommendation.get("pay_cents") is not None else None),
        },
        "adopted": _normalize_adopted(payload.get("adopted")),
        "modification": desensitize(modification) if modification is not None else None,
        "turns": int(payload.get("turns", 0) or 0),
        "latency_ms": int(payload.get("latency_ms", 0) or 0),
        "seed": (int(payload["seed"]) if payload.get("seed") is not None else None),
    }

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 逐行追加，单条独立成行，便于流式回放
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_events(path=DEFAULT_PATH, since=None):
    """读取 JSONL 全部记录；since 为 ISO 前缀（如 2026-10-25）时只返回该时刻之后。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and not rec.get("ts", "").startswith(since):
                continue
            out.append(rec)
    return out


def _percent(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _percentile(values, p):
    """线性插值百分位数；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac, 1)


def aggregate(records):
    """汇总指标，匹配 4.5：采纳率 / 二次修改率 / 时延 P50·P90。"""
    total = len(records)
    adopted_true = sum(1 for r in records if r.get("adopted") is True)
    adopted_pending = sum(1 for r in records if r.get("adopted") == "pending")
    modified = sum(1 for r in records if r.get("modification"))
    latencies = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    pays = [r["recommendation"]["pay_cents"] for r in records
            if r.get("recommendation", {}).get("pay_cents") is not None]
    return {
        "total": total,
        "adopted_true": adopted_true,
        "adopted_pending": adopted_pending,
        "adoption_rate_pct": _percent(adopted_true, total),
        "modification_count": modified,
        "modification_rate_pct": _percent(modified, total),
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p90": _percentile(latencies, 90),
        "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "pay_cents_min": min(pays) if pays else None,
        "pay_cents_max": max(pays) if pays else None,
    }


def _cmd_record(args):
    if args.json:
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            raise TelemetryError(f"--json 不是合法 JSON：{e}")
    elif args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise TelemetryError(f"读取 --input 失败：{e}")
    else:
        raise TelemetryError("record 需要 --json 或 --input 之一")
    rec = record_event(payload, path=args.path, session_id=args.session_id)
    print(json.dumps({"ok": True, "rec_id": rec["rec_id"], "path": args.path, "record": rec},
                     ensure_ascii=False, indent=2))


def _cmd_aggregate(args):
    records = read_events(args.path, since=args.since)
    print(json.dumps({"path": args.path, "summary": aggregate(records)},
                     ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="麦当劳推荐基线记录（脱敏埋点）")
    parser.add_argument("--path", default=DEFAULT_PATH, help=f"JSONL 路径，默认 {DEFAULT_PATH}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("record", help="记录一条基线事件")
    p_rec.add_argument("--json", help="事件 JSON 字符串")
    p_rec.add_argument("--input", help="事件 JSON 文件路径")
    p_rec.add_argument("--session-id", help="会话标识（同一会话内 rec_id 自增）")

    p_agg = sub.add_parser("aggregate", help="汇总采纳率/修改率/时延基线")
    p_agg.add_argument("--since", help="仅统计 ts 以此前缀开头的记录（如 2026-10-25）")

    args = parser.parse_args()
    try:
        if args.command == "record":
            _cmd_record(args)
        elif args.command == "aggregate":
            _cmd_aggregate(args)
    except TelemetryError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
