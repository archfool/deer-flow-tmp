"""保障检视对内与对客产物的统一中文展示格式。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_value(
    value: Decimal | None,
    unit: str,
    *,
    role: Literal["value", "existing", "target", "gap"] = "value",
) -> str:
    """将内核数字转换为中文展示单位，不改变业务口径。

    Args:
        value: 精确测算 Tool 返回的数值。
        unit: 诊断内核中的标准单位。
        role: 数值在报告中的语义角色，用于生成自然的医疗责任表达。

    Returns:
        适合中文报告的数字与单位。
    """

    if value is None:
        return "未知"
    if unit == "CNY":
        return f"{_compact_decimal(value / Decimal('10000'))} 万元"
    if unit == "CNY_PER_YEAR":
        return f"{_compact_decimal(value / Decimal('10000'))} 万元/年"
    if unit == "responsibility_tier":
        return _format_medical_responsibility(value, role)
    if unit == "months":
        return f"{_compact_decimal(value)} 个月"
    if unit == "years":
        return f"{_compact_decimal(value)} 年"
    return f"{_compact_decimal(value)} {unit}"


def _format_medical_responsibility(
    value: Decimal,
    role: Literal["value", "existing", "target", "gap"],
) -> str:
    """将医疗责任层级转换为代理人与客户可理解的业务表达。

    Args:
        value: 内核中的医疗责任层级数值。
        role: 当前数值表示普通值、已有安排、目标或缺口。

    Returns:
        不暴露内部层级编码的医疗责任说明。
    """

    descriptions = {
        Decimal("0"): {
            "value": "尚未配置商业医疗保障",
            "existing": "尚未配置商业医疗保障",
            "target": "当前阶段无需新增商业医疗保障",
            "gap": "当前无医疗责任缺口",
        },
        Decimal("1"): {
            "value": "基础住院医疗责任",
            "existing": "已覆盖基础住院医疗责任",
            "target": "建议覆盖基础住院医疗责任",
            "gap": "基础住院医疗责任待补足",
        },
        Decimal("2"): {
            "value": "基础及扩展医疗责任",
            "existing": "已覆盖基础及扩展医疗责任",
            "target": "建议覆盖基础及扩展医疗责任",
            "gap": "基础及扩展医疗责任待补足",
        },
    }
    if value in descriptions:
        return descriptions[value][role]

    count = _compact_decimal(value)
    templates = {
        "value": f"{count} 项医疗责任",
        "existing": f"已覆盖 {count} 项医疗责任",
        "target": f"建议覆盖 {count} 项医疗责任",
        "gap": f"尚有 {count} 项医疗责任待补足",
    }
    return templates[role]


def format_datetime(value: datetime) -> str:
    """把报告时间转换为中国时区的稳定展示格式。

    Args:
        value: 带时区的报告生成时间。

    Returns:
        `YYYY-MM-DD HH:MM` 格式的中国标准时间。
    """

    return value.astimezone(CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _compact_decimal(value: Decimal) -> str:
    """保留最多两位小数并添加千分位。

    Args:
        value: 待格式化的 Decimal。

    Returns:
        无多余尾零的千分位字符串。
    """

    return f"{value:,.2f}".rstrip("0").rstrip(".")
