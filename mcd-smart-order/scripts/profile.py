#!/usr/bin/env python3
"""麦当劳忌口档案：跨会话保存用户的忌口偏好，供所有模式共用。

档案默认保存在 ~/.workbuddy/mcd-profile.json，重新登录或换会话后仍然有效。

等级语义：
  strict    绝对不吃 —— 任何模式都不得出现在推荐里（转盘也不行）
  reluctant 勉强接受 —— 可以出现，但排序靠后并明确标注

命令：
    python profile.py show [--path FILE]
    python profile.py add --name 花生 --level strict [--keywords 花生,坚果] [--allergy]
    python profile.py remove --name 花生
    python profile.py effective [--path FILE]     # 输出供其他脚本使用的过滤参数
    python profile.py store set --code 1420450 --name "深圳福田站餐厅" --landmark "深圳·福田" --pickup "到店自取"
    python profile.py store show
    python profile.py store clear
"""

import argparse
import json
import os
import sys
from datetime import datetime

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcd-profile.json")
LEVELS = ("strict", "reluctant")
PICKUP_METHODS = ("到店自取", "麦乐送", "得来速")
EMPTY = {"version": 1, "updated_at": None, "restrictions": []}


class ProfileError(Exception):
    pass


def load(path):
    if not os.path.exists(path):
        return dict(EMPTY)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileError(f"档案读取失败：{e}")
    if not isinstance(data, dict) or "restrictions" not in data:
        raise ProfileError("档案格式不正确，缺少 restrictions 字段")
    return data


def save(data, path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find(data, name):
    for item in data["restrictions"]:
        if item.get("name") == name:
            return item
    return None


def cmd_show(data, _args):
    if not data["restrictions"]:
        print(json.dumps({"path_note": "尚无忌口记录", "restrictions": []}, ensure_ascii=False, indent=2))
        return
    print(json.dumps(
        {"updated_at": data.get("updated_at"), "restrictions": data["restrictions"]},
        ensure_ascii=False, indent=2))


def cmd_add(data, args):
    name = args.name.strip()
    if not name:
        raise ProfileError("忌口名称不能为空")
    if args.level not in LEVELS:
        raise ProfileError(f"level 只支持 {' 或 '.join(LEVELS)}")
    if find(data, name):
        raise ProfileError(f"已存在同名忌口：{name}，请先 remove 再 add")

    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    if not keywords:
        keywords = [name]

    data["restrictions"].append({
        "name": name,
        "level": args.level,
        "keywords": keywords,
        "allergy": bool(args.allergy),
    })
    return data


def cmd_remove(data, args):
    item = find(data, args.name.strip())
    if not item:
        raise ProfileError(f"未找到忌口：{args.name}")
    data["restrictions"].remove(item)
    return data


def cmd_effective(data, _args):
    """输出供 combo_optimizer / spin_wheel 直接使用的过滤参数。"""
    strict, reluctant, allergies = [], [], []
    for item in data["restrictions"]:
        keywords = item.get("keywords") or [item["name"]]
        if item.get("level") == "strict":
            strict.extend(keywords)
        else:
            reluctant.extend(keywords)
        if item.get("allergy"):
            allergies.append(item["name"])
    print(json.dumps(
        {
            "strict": sorted(set(strict)),
            "reluctant": sorted(set(reluctant)),
            "allergies": sorted(set(allergies)),
            "source": "profile",
        },
        ensure_ascii=False, indent=2))


# ---- 常去门店跨会话记忆 -----------------------------------------------------

def normalize_store(code, name, landmark, pickup):
    """校验并归一化门店记忆参数，返回 dict；不合法直接抛 ProfileError。"""
    code = (code or "").strip()
    name = (name or "").strip()
    landmark = (landmark or "").strip()
    pickup = (pickup or "").strip()
    if not code:
        raise ProfileError("门店 code 不能为空（来自 query-nearby-stores 的 storeCode）")
    if not name:
        raise ProfileError("门店名称不能为空，便于展示与用户核对")
    if pickup and pickup not in PICKUP_METHODS:
        raise ProfileError(f"pickup 仅支持 {' / '.join(PICKUP_METHODS)}")
    return {
        "store_code": code,
        "store_name": name,
        "landmark": landmark,
        "pickup": pickup or None,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def cmd_store_show(data, _args):
    store = data.get("favorite_store")
    if not store:
        print(json.dumps({"path_note": "尚未记住常去门店", "favorite_store": None},
                         ensure_ascii=False, indent=2))
        return
    print(json.dumps({"favorite_store": store}, ensure_ascii=False, indent=2))


def cmd_store_set(data, args):
    store = normalize_store(args.code, args.name, args.landmark, args.pickup)
    data["favorite_store"] = store
    return data


def cmd_store_clear(data, _args):
    if "favorite_store" in data:
        del data["favorite_store"]
    return data


def main():
    parser = argparse.ArgumentParser(description="麦当劳忌口档案")
    parser.add_argument("--path", default=DEFAULT_PATH, help=f"档案路径，默认 {DEFAULT_PATH}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="查看当前忌口")
    sub.add_parser("effective", help="输出供其他脚本使用的过滤参数")
    p_add = sub.add_parser("add", help="新增忌口")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--level", required=True, choices=LEVELS)
    p_add.add_argument("--keywords", help="逗号分隔的匹配关键词，缺省用名称")
    p_add.add_argument("--allergy", action="store_true", help="标记为过敏，提示会更严格")
    p_rm = sub.add_parser("remove", help="删除忌口")
    p_rm.add_argument("--name", required=True)

    # 常去门店记忆（跨会话）
    p_store = sub.add_parser("store", help="记住/查看/清除常去门店")
    store_sub = p_store.add_subparsers(dest="store_command", required=True)
    store_sub.add_parser("show", help="查看已记住的门店")
    p_store_clear = store_sub.add_parser("clear", help="清除已记住的门店")
    p_store_set = store_sub.add_parser("set", help="记住常去门店")
    p_store_set.add_argument("--code", required=True, help="门店 code（query-nearby-stores 的 storeCode）")
    p_store_set.add_argument("--name", required=True, help="门店名称")
    p_store_set.add_argument("--landmark", help="城市地标，如「深圳·福田」")
    p_store_set.add_argument("--pickup", help="默认取餐方式：" + " / ".join(PICKUP_METHODS))

    args = parser.parse_args()

    try:
        data = load(args.path)
        if args.command == "show":
            cmd_show(data, args)
            return
        if args.command == "effective":
            cmd_effective(data, args)
            return
        if args.command == "store":
            if args.store_command == "show":
                cmd_store_show(data, args)
                return
            if args.store_command == "clear":
                updated = cmd_store_clear(data, args)
                save(updated, args.path)
                print(json.dumps({"ok": True, "cleared": "favorite_store", "path": args.path},
                                 ensure_ascii=False, indent=2))
                return
            if args.store_command == "set":
                updated = cmd_store_set(data, args)
                save(updated, args.path)
                print(json.dumps({"ok": True, "favorite_store": updated["favorite_store"]},
                                 ensure_ascii=False, indent=2))
                return
        updated = cmd_add(data, args) if args.command == "add" else cmd_remove(data, args)
        save(updated, args.path)
        print(json.dumps({"ok": True, "path": args.path,
                          "restrictions": updated["restrictions"]}, ensure_ascii=False, indent=2))
    except ProfileError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
