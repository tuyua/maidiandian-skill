#!/usr/bin/env python3
"""profile.py 的离线测试：验证忌口档案的持久化与两级语义。

使用临时目录下的独立档案文件，不污染用户真实档案 ~/.workbuddy/mcd-profile.json。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "profile.py"

BASE = [
    {"code": "A1", "name": "板烧鸡腿堡", "price": 23.5, "category": "主食", "tags": ["单品"]},
    {"code": "A2", "name": "麦辣鸡腿汉堡", "price": 22.5, "category": "主食", "tags": ["单品"]},
    {"code": "A3", "name": "巨无霸", "price": 26.5, "category": "主食", "tags": ["单品", "100%纯牛肉"]},
    {"code": "B1", "name": "薯条", "price": 14.5, "category": "小食", "tags": ["小食"]},
    {"code": "B2", "name": "麦辣鸡翅", "price": 14.5, "category": "小食", "tags": ["单品"]},
    {"code": "C1", "name": "可乐", "price": 9.5, "category": "饮品", "tags": ["冷饮"]},
]


def run(args, path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(path)] + args,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_error": proc.stdout or proc.stderr, "_rc": proc.returncode}


def check(results, name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" -> {detail}"))


def main():
    results = []
    tmpdir = tempfile.mkdtemp(prefix="mcd_profile_test_")
    prof = os.path.join(tmpdir, "profile.json")

    # 1. 初始为空
    r = run(["show"], prof)
    check(results, "初始档案为空", r.get("restrictions") == [], r)

    # 2. 新增两级忌口
    r = run(["add", "--name", "牛肉", "--level", "strict", "--keywords", "牛肉,牛堡"], prof)
    check(results, "新增绝对不吃成功", r.get("ok") is True, r)
    r = run(["add", "--name", "辣", "--level", "reluctant"], prof)
    check(results, "新增勉强接受成功", r.get("ok") is True, r)

    # 3. 重复新增应报错
    r = run(["add", "--name", "牛肉", "--level", "strict"], prof)
    check(results, "重复忌口被拒绝", "error" in r, r)

    # 4. 非法等级应报错（argparse choices）
    proc = subprocess.run([sys.executable, str(SCRIPT), "--path", prof,
                           "add", "--name", "X", "--level", "maybe"],
                          capture_output=True, text=True, encoding="utf-8")
    check(results, "非法等级被拒绝", proc.returncode != 0)

    # 5. effective 输出两级关键词且分离
    r = run(["effective"], prof)
    check(results, "effective 正确分离两级关键词",
          set(r.get("strict", [])) == {"牛肉", "牛堡"} and r.get("reluctant") == ["辣"], r)

    # 6. 持久化：新进程读取仍在（模拟重新登录）
    r2 = run(["show"], prof)
    names = {i["name"] for i in r2.get("restrictions", [])}
    check(results, "持久化：重新读取仍在", names == {"牛肉", "辣"}, r2)

    # 7. 删除生效
    r = run(["remove", "--name", "辣"], prof)
    check(results, "删除忌口成功", r.get("ok") is True, r)
    r = run(["effective"], prof)
    check(results, "删除后 reluctant 为空", r.get("reluctant") == [], r)
    run(["add", "--name", "辣", "--level", "reluctant"], prof)

    # 8. 凑单：绝对不吃硬排除（含只在 tags 出现的关键词）
    payload = {
        "budget": 60, "people": 1, "goal": "rich", "max_items": 2,
        "exclude_keywords": ["牛肉", "牛堡"],
        "soft_exclude_keywords": ["辣"],
        "items": BASE,
    }
    fd, p1 = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "combo_optimizer.py"), "--input", p1, "--top", "5"],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    os.remove(p1)
    combo = json.loads(proc.stdout)
    all_names = [i["name"] for plan in combo.get("top_plans", []) for i in plan["items"]]
    check(results, "凑单：绝对不吃被硬排除（含 tags 命中）",
          "巨无霸" not in all_names and all("牛肉" not in n for n in all_names), all_names)
    check(results, "凑单：含勉强接受的方案排在无勉强项之后",
          [p["has_reluctant"] for p in combo["top_plans"]] == sorted([p["has_reluctant"] for p in combo["top_plans"]]),
          [p["has_reluctant"] for p in combo["top_plans"]])

    # 9. 转盘：绝对不吃的餐品不进候选池
    wheel_payload = dict(payload)
    wheel_payload["top"] = 6
    fd, p2 = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(wheel_payload, f, ensure_ascii=False)
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "spin_wheel.py"),
                           "--input", p2, "--top", "5", "--seed", "1"],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    os.remove(p2)
    wheel = json.loads(proc.stdout)
    picked_names = [i["name"] for i in wheel.get("picked", [])]
    check(results, "转盘：绝对不吃不进候选池", "巨无霸" not in picked_names, picked_names)
    check(results, "转盘：勉强接受项被标注",
          all(("辣" not in n) or i["reluctant_matches"] for n, i in zip(picked_names, wheel.get("picked", []))),
          wheel.get("picked"))

    # 清理
    for f in os.listdir(tmpdir):
        os.remove(os.path.join(tmpdir, f))
    os.rmdir(tmpdir)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项检查，失败 {len(failed)} 项")
    if failed:
        print("失败项：", "、".join(failed))
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
