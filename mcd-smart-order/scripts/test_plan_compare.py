#!/usr/bin/env python3
"""plan_compare 三选一对比的离线测试：覆盖三视图、推荐默认、价格关系、无解降级。"""

import json
import subprocess
import sys
import tempfile
import os

HERE = __file__[: __file__.rfind("/")] if "/" in __file__ else "."
if os.sep != "/":
    HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from combo_optimizer import plan_once  # 复用基础校验，确认无 ImportError

SCRIPT = os.path.join(HERE, "plan_compare.py")

# 内联小样本：价格/分类/热量都确定，保证测试不漂移
SAMPLE = {
    "budget": 35,
    "people": 1,
    "items": [
        {"code": "M1", "name": "巨无霸", "price": 24.0, "category": "主食", "kcal": 563},
        {"code": "M2", "name": "麦辣鸡腿堡", "price": 21.0, "category": "主食", "kcal": 517},
        {"code": "M3", "name": "薯条", "price": 12.0, "category": "小食", "kcal": 289},
        {"code": "M4", "name": "玉米杯", "price": 9.0, "category": "小食", "kcal": 112},
        {"code": "M5", "name": "小可乐", "price": 9.5, "category": "饮品", "kcal": 150},
        {"code": "M6", "name": "苹果片", "price": 7.0, "category": "甜品", "kcal": 35},
    ],
}

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
    assert rc == 0, f"退出码 {rc}; stderr={err[-400:]}"
    return json.loads(out)


# 1. 三视图都能出结果
res = load(SAMPLE)
views = {v["goal"]: v for v in res["views"]}
check("返回三个视图（rich/cheap/lean）", set(views) == {"rich", "cheap", "lean"},
      set(views))
check("rich/cheap/lean 均有方案（小样本约束宽松）",
      all(v["plan"] for v in views.values()),
      {g: v.get("error") for g, v in views.items()})

# 2. 默认推荐是主推 rich（三套都可用时）
check("默认推荐为 rich", res["diff"]["recommend"] == "rich", res["diff"]["recommend"])

# 3. 省钱方案价格 <= 主推方案价格
rich_p = views["rich"]["plan"]["total_price"]
cheap_p = views["cheap"]["plan"]["total_price"]
check("省钱方案价格不高于主推", cheap_p <= rich_p + 1e-9,
      f"cheap={cheap_p} rich={rich_p}")

# 4. 价格差 delta 计算正确
spread = res["diff"]["price_spread"]
check("price_spread.delta == max-min", abs(spread["delta"] - round(rich_p - cheap_p, 2)) < 1e-9,
      spread)

# 5. 每张方案带「为什么选它」理由，且非空
for g, v in views.items():
    check(f"{g} 视图有非空 reason", bool(v.get("reason")), v.get("reason"))

# 6. lean 方案热量可核实时，理由里出现 kcal 字样
lean_reason = views["lean"]["reason"]
check("lean 理由含热量说明", "kcal" in lean_reason, lean_reason)

# 7. 预算极低 + 热量严控：lean 可能无解，但整体不崩溃，且标为不可用
hard = dict(SAMPLE, budget=20, max_kcal=50)
res_hard = load(hard)
hard_views = {v["goal"]: v for v in res_hard["views"]}
check("极端约束下脚本整体不崩溃", "views" in res_hard and "diff" in res_hard,
      res_hard.get("error"))
lean_unavail = hard_views["lean"]["plan"] is None
check("lean 在 max_kcal=50 下标记无解而非崩溃",
      lean_unavail and "无解" in (hard_views["lean"].get("reason") or ""),
      hard_views["lean"])

# 8. 预算缺失 → 退出码非 0 + error_code invalid_param
rc, out, err = run({"people": 1, "items": SAMPLE["items"]})
check("缺失 budget → invalid_param", rc != 0 and "invalid_param" in (out + err),
      f"rc={rc}")

# 9. diff.available 只收录有解的目标
check("diff.available 与视图可用性一致",
      set(res["diff"]["available"]) == {g for g, v in views.items() if v["plan"]},
      res["diff"]["available"])


print(f"\n共 {passed + failed} 项检查，失败 {failed} 项")
if failed:
    print("RESULT: HAS FAILURE")
    print("失败项:", fails)
    sys.exit(1)
print("RESULT: ALL PASS")
