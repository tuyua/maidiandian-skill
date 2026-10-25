#!/usr/bin/env python3
"""profile.py store 子命令的离线测试：覆盖设置/读取/清除/校验，且用临时文件避免污染真实档案。"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "profile.py")

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


def run_store(*cli_args, path):
    proc = subprocess.run([sys.executable, SCRIPT, "--path", path, "store", *cli_args],
                          capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stdout, proc.stderr


def read_profile(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# 所有用例共用一个临时档案，验证「跨会话」写入后能读回。
# 不要预建文件：load() 对「文件不存在」返回空档案，对空文件反而报错。
tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, dir=HERE)
tmp.close()
os.remove(tmp.name)  # 删掉空壳，让首个命令走「不存在→空档案」分支
path = tmp.name
try:
    # 1. 初始无门店
    rc, out, err = run_store("show", path=path)
    data = json.loads(out)
    check("初始无门店记忆", rc == 0 and data.get("favorite_store") is None, out[-200:])

    # 2. 设置一个门店
    rc, out, err = run_store("set", "--code", "1420450", "--name", "深圳福田站餐厅",
                             "--landmark", "深圳·福田", "--pickup", "到店自取", path=path)
    check("设置门店成功", rc == 0 and "ok" in json.loads(out), err[-200:] or out[-200:])
    data = read_profile(path)
    store = data.get("favorite_store") or {}
    check("写入字段完整", store.get("store_code") == "1420450"
          and store.get("store_name") == "深圳福田站餐厅"
          and store.get("pickup") == "到店自取", store)

    # 3. 跨会话读取（再次 show）
    rc, out, err = run_store("show", path=path)
    data = json.loads(out)
    check("跨会话读回门店", data.get("favorite_store", {}).get("store_code") == "1420450",
          data)

    # 4. 覆盖更新
    rc, out, err = run_store("set", "--code", "778899", "--name", "广州天河城餐厅",
                             "--landmark", "广州·天河", path=path)
    data = read_profile(path)
    check("覆盖更新生效", data["favorite_store"]["store_code"] == "778899", data)

    # 5. pickup 非法值被拒
    rc, out, err = run_store("set", "--code", "1", "--name", "测试店", "--pickup", "闪送",
                             path=path)
    check("非法 pickup 被拒", rc != 0 and "pickup" in (out + err), f"rc={rc}")

    # 6. code 缺失被拒
    rc, out, err = run_store("set", "--name", "无码店", path=path)
    check("code 缺失被拒", rc != 0 and "code" in (out + err), f"rc={rc}")

    # 7. 清除后变回 None
    rc, out, err = run_store("clear", path=path)
    data = read_profile(path)
    check("清除后 favorite_store 不存在", rc == 0 and "favorite_store" not in data, data)

    # 8. 不带 pickup 也能设置（可选）
    rc, out, err = run_store("set", "--code", "555", "--name", "只记code店", path=path)
    data = read_profile(path)
    check("可选 pickup：仅记 code/name 成功", rc == 0 and data["favorite_store"]["pickup"] is None,
          data)

finally:
    if os.path.exists(path):
        os.remove(path)


print(f"\n共 {passed + failed} 项检查，失败 {failed} 项")
if failed:
    print("RESULT: HAS FAILURE")
    print("失败项:", fails)
    sys.exit(1)
print("RESULT: ALL PASS")
