#!/usr/bin/env python3
"""mcd-smart-order 脚本共用工具：金额定点转换与入参校验。

被 combo_optimizer.py 与 spin_wheel.py 共用，避免两套校验逻辑出现分歧。
"""

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


NON_FOOD_CATEGORY = "周边"

# 实测在真实 query-meals 里出现过的非食品编码，它们会被嵌在食品分类里返回
NON_FOOD_CODES = {"521956", "521744"}

# 名称/标签命中任一关键词即判定为非食品，与它被归到哪个麦当劳分类无关
NON_FOOD_HINTS = ("丝巾", "周边", "毛绒", "玩具", "公仔", "玩偶", "礼品卡", "水壶", "保温杯", "钥匙扣", "徽章", "手提袋")


def normalize_tags(item):
    """统一把 tags 解析成字符串列表，兼容字符串写法。"""
    raw = item.get("tags")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        return [raw]
    return []


def resolve_category(item, default="其他"):
    """判定餐品分类，并**确定性地**识别非食品（周边）。

    真实 query-meals 会把非食品嵌进食品分类：丝巾在「人气热卖」、毛绒周边在
    「小食甜品/其他」。若依赖调用方做分类映射，命中不了 `exclude_categories`
    就会把丝巾当餐品推荐出去。因此这里按已知编码与名称关键词强制改写分类，
    无论调用方把它标成什么都归入「周边」。

    返回 (category, is_non_food)。is_non_food 为真时调用方必须丢弃该条目。
    """
    code = str(item.get("code", "")).strip()
    name = str(item.get("name", ""))
    haystack = name + " " + " ".join(normalize_tags(item))
    if code in NON_FOOD_CODES or any(hint in haystack for hint in NON_FOOD_HINTS):
        return NON_FOOD_CATEGORY, True
    return str(item.get("category") or default), False


class InputError(Exception):
    """入参不合法。调用方必须把错误告知用户，不得继续推荐。"""


def to_cents(value):
    """金额转分：定点四舍五入到分，拒绝 NaN、无穷与负数。"""
    if isinstance(value, bool):
        raise InputError("金额不能是布尔值")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        raise InputError(f"金额必须是数字：{value!r}")
    if not amount.is_finite():
        raise InputError(f"金额必须是有限数字：{value!r}")
    if amount < 0:
        raise InputError(f"金额不能为负数：{value!r}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def as_int(value, field, minimum, maximum):
    """校验整数参数：拒绝布尔值、非整数、超范围。"""
    if isinstance(value, bool):
        raise InputError(f"{field} 必须是整数")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InputError(f"{field} 必须是整数：{value!r}")
    if not math.isfinite(number) or number != int(number):
        raise InputError(f"{field} 必须是整数：{value!r}")
    number = int(number)
    if number < minimum or number > maximum:
        raise InputError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return number


def parse_kcal(value):
    """餐品热量：缺失或非法一律返回 (None, True)，绝不参与求和。"""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return None, True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(number) or number < 0:
        return None, True
    return number, False


def parse_non_negative(value, field):
    """非负数校验，0 合法。"""
    if isinstance(value, bool):
        raise InputError(f"{field} 不能是布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InputError(f"{field} 必须是数字：{value!r}")
    if not math.isfinite(number) or number < 0:
        raise InputError(f"{field} 必须是有限非负数：{value!r}")
    return number
