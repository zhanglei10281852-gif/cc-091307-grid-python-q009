"""领域模型：报修单、工单（处理链）、班组、事件与验收记录。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


def normalize_facility_key(region: str, address: str, category: str) -> str:
    """设施唯一键：区域 + 具体位置 + 类别，用于识别同一设施的重复报修。"""
    parts = (region, address, category)
    return "|".join(" ".join(p.split()).lower() for p in parts)


class RiskLevel(enum.IntEnum):
    """风险等级，数值越大越紧急。"""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4

    @property
    def label(self) -> str:
        return _RISK_LABELS[self]

    @classmethod
    def from_label(cls, label: str) -> "RiskLevel":
        for level, text in _RISK_LABELS.items():
            if text == label:
                return level
        raise ValueError(f"未知风险等级: {label!r}")


_RISK_LABELS = {
    RiskLevel.LOW: "低",
    RiskLevel.MEDIUM: "中",
    RiskLevel.HIGH: "高",
    RiskLevel.URGENT: "紧急",
}

#: 各风险等级的处置时限（小时），超时即列入管理端超时清单。
SLA_HOURS = {
    RiskLevel.LOW: 168,
    RiskLevel.MEDIUM: 72,
    RiskLevel.HIGH: 24,
    RiskLevel.URGENT: 4,
}

#: 派工优先级权重：紧急故障获得最高权重，作为派工先后的依据。
PRIORITY_WEIGHT = {
    RiskLevel.LOW: 10,
    RiskLevel.MEDIUM: 40,
    RiskLevel.HIGH: 70,
    RiskLevel.URGENT: 100,
}

#: 常见设施类别（不限制其他类别）。
COMMON_CATEGORIES = ("路灯", "健身器材", "楼道扶手")


class ReportStatus(str, enum.Enum):
    PENDING = "待派工"
    ACTIVE = "处理中"
    RESOLVED = "已完成"
    MERGED = "已合并"


class OrderStatus(str, enum.Enum):
    """工单（处理链）状态机。"""

    PENDING = "待派工"        # 已建单但未匹配到班组
    DISPATCHED = "已派工"      # 已派工，班组未接单
    IN_PROGRESS = "整改中"     # 班组已接单，整改阶段
    PENDING_ACCEPTANCE = "待验收"
    COMPLETED = "已完成"       # 验收通过
    PAUSED = "已暂停"
    CANCELLED = "已关闭"       # 因合并而关闭


#: 仍处于处理链上的工单状态（用于统计班组负荷、超时）。
ACTIVE_ORDER_STATUSES = (
    OrderStatus.DISPATCHED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.PAUSED,
    OrderStatus.PENDING_ACCEPTANCE,
)


class EventType:
    """事件类型。报修单事件以 REPORT_ 前缀语义区分，工单事件构成处理链。"""

    # 报修单事件
    REPORT_CREATED = "REPORT_CREATED"        # 报修受理
    SUPPLEMENTED = "SUPPLEMENTED"            # 补充
    RISK_CHANGED = "RISK_CHANGED"            # 风险等级调整
    MERGED = "MERGED"                        # 本单被合并
    ABSORBED_SOURCE = "ABSORBED_SOURCE"      # 本单吸收了来源单
    REPORT_RESOLVED = "REPORT_RESOLVED"      # 报修完成

    # 工单事件（处理链）
    DISPATCHED = "DISPATCHED"                # 派工
    DISPATCH_FAILED = "DISPATCH_FAILED"      # 无匹配班组
    PRIORITY_CHANGED = "PRIORITY_CHANGED"    # 优先级调整
    TRANSFERRED = "TRANSFERRED"              # 转交
    PAUSED = "PAUSED"                        # 暂停
    RESUMED = "RESUMED"                      # 恢复
    START_WORK = "START_WORK"                # 班组接单开工（移动端事件）
    PROGRESS_NOTE = "PROGRESS_NOTE"          # 进度反馈（移动端事件）
    SUBMIT_ACCEPTANCE = "SUBMIT_ACCEPTANCE"  # 提交验收（移动端事件）
    ACCEPTED = "ACCEPTED"                    # 验收通过
    ACCEPTANCE_FAILED = "ACCEPTANCE_FAILED"  # 验收不通过
    MERGE_CLOSED = "MERGE_CLOSED"            # 因合并关闭
    MERGED_SOURCE = "MERGED_SOURCE"          # 吸收了来源工单


#: 班组移动端可上报的事件及其允许的状态迁移。
#: 验收不通过只能回到整改阶段，由 ACCEPTANCE_FAILED 的迁移表保证。
CREW_EVENT_TRANSITIONS = {
    EventType.START_WORK: {OrderStatus.DISPATCHED: OrderStatus.IN_PROGRESS},
    EventType.PROGRESS_NOTE: {OrderStatus.IN_PROGRESS: OrderStatus.IN_PROGRESS},
    EventType.SUBMIT_ACCEPTANCE: {OrderStatus.IN_PROGRESS: OrderStatus.PENDING_ACCEPTANCE},
}


class Role(str, enum.Enum):
    """岗位角色，决定联系方式可见级别与管理端权限。"""

    ADMIN = "系统管理员"
    DISPATCHER = "调度员"
    MANAGER = "管理端"
    CREW = "班组"
    VIEWER = "访客"


class ContactVisibility(enum.IntEnum):
    HIDDEN = 0   # 完全隐藏
    MASKED = 1   # 脱敏展示
    FULL = 2     # 完整可见


#: 各岗位对报修人联系方式的可见级别。
CONTACT_PERMISSIONS = {
    Role.ADMIN: ContactVisibility.FULL,
    Role.DISPATCHER: ContactVisibility.FULL,
    Role.MANAGER: ContactVisibility.MASKED,
    Role.CREW: ContactVisibility.MASKED,
    Role.VIEWER: ContactVisibility.HIDDEN,
}

#: 可查看管理端总览的角色。
MANAGEMENT_ROLES = (Role.ADMIN, Role.MANAGER, Role.DISPATCHER)


@dataclass
class Report:
    """报修单：受理留痕与合并回溯的载体。"""

    id: str
    category: str
    region: str
    address: str
    risk_level: RiskLevel
    photo_summary: str
    reporter_name: str
    reporter_phone: str
    status: ReportStatus
    created_at: datetime
    updated_at: datetime
    merged_into: Optional[str] = None

    @property
    def facility_key(self) -> str:
        return normalize_facility_key(self.region, self.address, self.category)


@dataclass
class WorkOrder:
    """工单：一次报修的可追踪处理链。"""

    id: str
    report_id: str
    crew_id: Optional[str]
    status: OrderStatus
    priority: int
    priority_reason: str
    last_seq: int
    created_at: datetime
    updated_at: datetime
    paused_from: Optional[OrderStatus] = None
    completed_at: Optional[datetime] = None


@dataclass
class Crew:
    """维修班组：按类别与区域匹配派工。"""

    id: str
    name: str
    categories: tuple[str, ...]
    regions: tuple[str, ...]
    max_parallel: int = 5


@dataclass
class Event:
    """事件：所有状态变更的留痕，工单事件按序号幂等。"""

    id: int
    aggregate_type: str   # "report" | "order"
    aggregate_id: str
    seq: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    result: Optional[dict[str, Any]]
    created_at: datetime


@dataclass
class AcceptanceRecord:
    """验收记录：每次验收的意见留痕。"""

    id: int
    order_id: str
    round: int
    passed: bool
    opinion: str
    inspector: str
    created_at: datetime
