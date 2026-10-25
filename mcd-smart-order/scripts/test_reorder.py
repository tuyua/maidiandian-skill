#!/usr/bin/env python3
"""reorder.py 离线测试：覆盖在售/下架/涨价/按名称匹配/复购 payload 生成。"""

import json
import subprocess
import sys
import tempfile
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "reorder.py")

passed = 0
failed = 0
fails = []


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        fails.append(name)
        print(f"  [FAIL] {name} → {detail}")


def run(payload):
    fd, path = tempfile.mkstemp(suffix=".json", dir=HERE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    try:
        proc = subprocess.run([sys.executable, SCRIPT, "--input", path],
                              capture_output=True, text=True, encoding="utf-8")
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        os.remove(path)


def load(payload):
    rc, out, err = run(payload)
    assert rc == 0, f"rc={rc}; err={err[-400:]}"
    return json.loads(out)


PAST = [
    {"code": "M1", "name": "巨无霸", "price": 24.0, "quantity": 1},
    {"code": "M3", "name": "薯条", "price": 12.0, "quantity": 2},
    {"code": "OLD1", "name": "限时樱花筒", "price": 8.0, "quantity": 1},   # 已下架
    {"code": "M9", "name": "麦辣鸡腿堡（旧名）", "price": 21.0, "quantity": 1},  # 现名不同，按名匹配
]
MENU = [
    {"code": "M1", "name": "巨无霸", "price": 26.0, "category": "主食", "kcal": 563},
    {"code": "M3", "name": "薯条", "price": 12.0, "category": "小食", "kcal": 289},
    {"code": "M2", "name": "麦辣鸡腿堡", "price": 21.5, "category": "主食", "kcal": 517},
]

res = load({"past_order": PAST, "current_menu": MENU})

by_name = {a["name"]: a for a in res["available"]}
by_disc = {d["name"]: d for d in res["discontinued"]}

# 1. 在售计数正确（巨无霸/薯条/麦辣鸡腿堡 3 项）
check("在售 3 项", len(res["available"]) == 3, res["available"])
# 2. 已下架 1 项（限时樱花筒）
check("已下架 1 项", len(res["discontinued"]) == 1, res["discontinued"])
# 3. 巨无霸涨价识别
check("巨无霸识别为涨价 +2 元", by_name["巨无霸"]["status"] == "price_up"
      and by_name["巨无霸"]["delta"] == 2.0, by_name["巨无霸"])
# 4. 薯条价格不变
check("薯条识别为 unchanged", by_name["薯条"]["status"] == "unchanged", by_name["薯条"])
# 5. 旧名下架项按名称近似匹配到现名（晋升为在售，但标记名称有变）
check("旧名麦辣鸡腿堡（旧名）按名称匹配到在售", "麦辣鸡腿堡" in by_name,
      res["available"])
check("近似匹配标记为 name_approx 且 name_changed",
      by_name.get("麦辣鸡腿堡", {}).get("matched_by") == "name_approx"
      and by_name.get("麦辣鸡腿堡", {}).get("name_changed") is True,
      by_name.get("麦辣鸡腿堡"))
# 6. 已下架项给出替代建议
check("已下架项给出 suggested_replacement 或为空", "suggested_replacement" in by_disc["限时樱花筒"],
      by_disc["限时樱花筒"])
# 7. 复购 payload 仅含在售项，数量正确
items = res["reorder_payload"]["items"]
codes = {i["code"] for i in items}
check("复购 payload 不含下架 code", "OLD1" not in codes, codes)
check("薯条数量为 2（含加倍）", any(i["code"] == "M3" and i["quantity"] == 2 for i in items),
      items)
# 8. 复购合计 = 26 + 12*2 + 21.5 = 71.5
check("复购现价合计正确 71.5", abs(res["reorder_payload"]["total_now"] - 71.5) < 1e-9,
      res["reorder_payload"]["total_now"])
# 9. preview_budget 给余量且 > total
check("preview_budget 含余量", res["reorder_payload"]["preview_budget"] > 71.5,
      res["reorder_payload"]["preview_budget"])
# 10. 摘要含关键数字
check("summary 含在售/下架计数", "在售 3 项" in res["summary"] and "已下架 1 项" in res["summary"],
      res["summary"])

# 11. 空 past_order 被拒
rc, out, err = run({"past_order": [], "current_menu": MENU})
check("空 past_order → invalid_param", rc != 0 and "invalid_param" in (out + err), f"rc={rc}")

# 12. current_menu 缺失也可运行（全部判为下架，不崩溃）
res_empty = load({"past_order": PAST})
check("无菜单时不崩溃且全部下架", res_empty["available"] == []
      and len(res_empty["discontinued"]) == 4, res_empty)


print(f"\n共 {passed + failed} 项检查，失败 {failed} 项")
if failed:
    print("RESULT: HAS FAILURE")
    print("失败项:", fails)
    sys.exit(1)
print("RESULT: ALL PASS")
