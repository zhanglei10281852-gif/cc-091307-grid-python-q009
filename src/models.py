"""领域枚举与值对象。"""
from __future__ import annotations

import enum


class Risk(enum.IntEnum):
    """风险等级（含默认值 UNKNOWN，数值越大越紧急）。"""

    UNKNOWN = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: object) -> "Risk":
        if isinstance(value, Risk):
            return value
        if value is None:
            return cls.UNKNOWN
        if isinstance(value, int) and not isinstance(value, bool):
            return cls._value2member_map_.get(value, cls.UNKNOWN)  # type: ignore[return-value]
        text = str(value).strip().upper()
        aliases = {
            "低": cls.LOW,
            "中": cls.MEDIUM,
            "高": cls.HIGH,
            "紧急": cls.CRITICAL,
            "CRITICAL": cls.CRITICAL,
            "HIGH": cls.HIGH,
            "MEDIUM": cls.MEDIUM,
            "LOW": cls.LOW,
            "NORMAL": cls.LOW,
            "URGENT": cls.CRITICAL,
        }
        return aliases.get(text, cls.UNKNOWN)


class Category(enum.Enum):
    """设施类别。"""

    STREETLIGHT = "streetlight"
    FITNESS = "fitness"
    HANDRAIL = "handrail"
    OTHER = "other"

    @classmethod
    def parse(cls, value: object) -> "Category":
        if isinstance(value, Category):
            return value
        text = str(value).strip().lower() if value is not None else ""
        aliases = {
            "streetlight": cls.STREETLIGHT,
            "light": cls.STREETLIGHT,
            "lamp": cls.STREETLIGHT,
            "路灯": cls.STREETLIGHT,
            "fitness": cls.FITNESS,
            "gym": cls.FITNESS,
            "健身": cls.FITNESS,
            "健身器材": cls.FITNESS,
            "handrail": cls.HANDRAIL,
            "railing": cls.HANDRAIL,
            "扶手": cls.HANDRAIL,
            "楼道扶手": cls.HANDRAIL,
            "": cls.OTHER,
        }
        return aliases.get(text, cls.OTHER)


class State(enum.Enum):
    """工单处理链状态。

    流转规则：
        PENDING -> DISPATCHED -> IN_PROGRESS -> RECTIFYING -> IN_PROGRESS ... -> ACCEPTED
        任意活跃状态可 PAUSED；暂停后只能恢复到暂停前状态；
        任何状态都可能被 MERGED（合入其他工单）或 CLOSED_DUP（关闭重复件）。
    """

    PENDING = "pending"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    RECTIFYING = "rectifying"
    PENDING_ACCEPTANCE = "pending_acceptance"
    ACCEPTED = "accepted"
    MERGED = "merged"
    CLOSED_DUP = "closed_dup"


# 各设施类别与风险等级对应的 SLA（小时）：无 SLA 时表示不考核超时。
SLA_HOURS: dict[tuple[Category, Risk], float] = {
    (Category.STREETLIGHT, Risk.CRITICAL): 4,
    (Category.STREETLIGHT, Risk.HIGH): 8,
    (Category.STREETLIGHT, Risk.MEDIUM): 24,
    (Category.STREETLIGHT, Risk.LOW): 72,
    (Category.FITNESS, Risk.CRITICAL): 4,
    (Category.FITNESS, Risk.HIGH): 12,
    (Category.FITNESS, Risk.MEDIUM): 36,
    (Category.FITNESS, Risk.LOW): 72,
    (Category.HANDRAIL, Risk.CRITICAL): 6,
    (Category.HANDRAIL, Risk.HIGH): 12,
    (Category.HANDRAIL, Risk.MEDIUM): 36,
    (Category.HANDRAIL, Risk.LOW): 72,
    (Category.OTHER, Risk.CRITICAL): 24,
    (Category.OTHER, Risk.HIGH): 48,
    (Category.OTHER, Risk.MEDIUM): 72,
    (Category.OTHER, Risk.LOW): 120,
}

RISK_WEIGHT = {
    Risk.UNKNOWN: 1.0,
    Risk.LOW: 1.0,
    Risk.MEDIUM: 2.0,
    Risk.HIGH: 3.0,
    Risk.CRITICAL: 5.0,
}
