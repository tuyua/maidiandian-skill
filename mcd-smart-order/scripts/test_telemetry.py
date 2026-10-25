#!/usr/bin/env python3
"""telemetry.py 离线测试：脱敏落盘、rec_id 自增、adopted 校验、聚合指标、无明文。

全部在临时文件运行，不写入用户真实 ~/.workbuddy/mcd-telemetry.jsonl。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "telemetry.py")


def run_record(payload, path, session_id=None):
    # --path 是顶层参数，必须写在子命令 record 之前（与 profile.py 约定一致）
    args = [sys.executable, SCRIPT, "--path", path, "record"]
    if session_id:
        args += ["--session-id", session_id]
    args += ["--json", json.dumps(payload, ensure_ascii=False)]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise AssertionError(f"record 失败 rc={r.returncode} stderr={r.stderr}")
    return json.loads(r.stdout)["record"]


def read_all(path):
    r = subprocess.run([sys.executable, SCRIPT, "--path", path, "aggregate"],
                       capture_output=True, text=True, encoding="utf-8")
    return json.loads(r.stdout)["summary"]


class TelemetryTest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    # ---- 基本写入与 rec_id 自增 ----
    def test_rec_id_increments_per_session(self):
        run_record({"turns": 1, "latency_ms": 100}, self.path, session_id="S1")
        a = run_record({"turns": 2, "latency_ms": 200}, self.path, session_id="S1")
        b = run_record({"turns": 3, "latency_ms": 300}, self.path, session_id="S2")
        self.assertEqual(a["rec_id"], 2)          # S1 内第二条 = 2
        self.assertEqual(b["rec_id"], 1)          # S2 第一条 = 1（按会话隔离）

    # ---- 脱敏：敏感字段与嵌入手机号 ----
    def test_constraints_desensitized(self):
        rec = run_record({
            "constraints": {
                "budget": 35,
                "name": "张三",
                "phone": "13800138000",
                "address": "北京市朝阳区某小区3号楼",
                "store": {"store_name": "深圳福田站餐厅", "full_address": "深圳市福田区xx路1号"},
            }
        }, self.path)
        c = rec["constraints"]
        self.assertEqual(c["name"], "[已脱敏]")
        self.assertEqual(c["phone"], "138****8000")       # 电话打码保留前3后4
        self.assertEqual(c["address"], "[已脱敏]")
        self.assertEqual(c["store"]["store_name"], "深圳福田站餐厅")  # 非完整地址字段保留
        self.assertEqual(c["store"]["full_address"], "[已脱敏]")
        self.assertEqual(c["budget"], 35)

    def test_embedded_phone_in_string_masked(self):
        rec = run_record({"constraints": {"note": "联系13812345678确认"}}, self.path)
        self.assertIn("138****5678", rec["constraints"]["note"])
        self.assertNotIn("13812345678", rec["constraints"]["note"])

    def test_no_plaintext_sensitive_on_disk(self):
        run_record({
            "constraints": {"name": "李四", "phone": "13900139000"},
            "modification": {"field": "address", "from": "杭州西湖区xx", "to": "上海浦东xx"},
        }, self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("13900139000", raw)   # 明文手机号不得落盘
        self.assertNotIn("李四", raw)
        self.assertIn("139****9000", raw)      # 已打码形式出现

    # ---- adopted 校验 ----
    def test_adopted_values(self):
        t = run_record({"adopted": True}, self.path)["adopted"]
        f = run_record({"adopted": False}, self.path)["adopted"]
        p = run_record({"adopted": "pending"}, self.path)["adopted"]
        self.assertIs(t, True)
        self.assertIs(f, False)
        self.assertEqual(p, "pending")

    def test_adopted_invalid_rejected(self):
        # --path 是顶层参数，必须写在 record 之前
        args = [sys.executable, SCRIPT, "--path", self.path, "record",
                "--json", json.dumps({"adopted": "maybe"})]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error", json.loads(r.stdout))

    # ---- 聚合指标 ----
    def test_aggregate_metrics(self):
        for i, (adopted, lat) in enumerate([(True, 100), (False, 200), (True, 300),
                                            ("pending", 400), (False, 500)]):
            run_record({"adopted": adopted, "latency_ms": lat,
                        "recommendation": {"pay_cents": 3500 + i * 100}},
                       self.path)
        s = read_all(self.path)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["adopted_true"], 2)
        self.assertEqual(s["adopted_pending"], 1)
        self.assertEqual(s["adoption_rate_pct"], 40.0)     # 2/5 已采纳
        self.assertEqual(s["modification_count"], 0)
        self.assertEqual(s["latency_ms_p50"], 300.0)        # 中位数
        self.assertEqual(s["latency_ms_p90"], 460.0)        # 线性插值 P90
        self.assertEqual(s["pay_cents_min"], 3500)
        self.assertEqual(s["pay_cents_max"], 3900)


if __name__ == "__main__":
    unittest.main(verbosity=2)
