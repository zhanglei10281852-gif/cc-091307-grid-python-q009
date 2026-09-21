"""领域事件：所有状态变更都以不可变事件落库。

每个事件携带：
* seq        : 工单内单调递增的事件序号（乐观锁版本，进度更新必须携带）；
* client_id  : 班组移动端设备/客户端标识；
* client_seq : 该客户端自己分配的事件序号，用于离线重发的幂等去重。
"""
from __future__ import annotations

import dataclasses
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Event:
    """事件基类。"""

    ticket_id: str
    seq: int
    actor: str
    ts: float = field(default_factory=_now)
    client_id: str | None = None
    client_seq: int | None = None
    # 幂等命中时不产生新事件，由存储层回填原事件 id；正常事件自动生成。
    event_id: str = field(default_factory=_new_id)

    @property
    def etype(self) -> str:
        return type(self).__name__

    def payload(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        for key in ("ticket_id", "seq", "actor", "ts", "client_id", "client_seq", "event_id"):
            data.pop(key, None)
        return data

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ticket_id": self.ticket_id,
            "seq": self.seq,
            "etype": self.etype,
            "actor": self.actor,
            "ts": self.ts,
            "client_id": self.client_id,
            "client_seq": self.client_seq,
            "payload": json.dumps(self.payload(), ensure_ascii=False),
        }


# ---- 报修来源与工单生命周期事件 ----

@dataclass(frozen=True)
class Reported(Event):
    """新报修登记（可能是主工单，也可能作为被合并件 later-merge）。"""

    location: str = ""
    region: str = ""
    category: str = "other"
    risk: int = 0
    photo_summary: str = ""
    reporter_name: str = ""
    reporter_contact: str = ""
    source_id: str = ""  # 报修来源流水号（合并后回溯原始来源的稳定标识）


@dataclass(frozen=True)
class Supplemented(Event):
    """报修信息被补充（照片摘要、联系方式、风险等级等）。"""

    photo_summary: str = ""
    reporter_contact: str = ""
    risk: int | None = None
    note: str = ""


@dataclass(frozen=True)
class Dispatched(Event):
    """派工到班组。"""

    crew_id: str = ""
    region: str = ""
    risk: int = 0
    reason: str = ""


@dataclass(frozen=True)
class ProgressPosted(Event):
    """班组上报进度（移动端离线/重发的高频事件）。"""

    status_text: str = ""
    phase: str = ""  # dispatched / in_progress / rectifying
    percent: int = 0


@dataclass(frozen=True)
class Transferred(Event):
    """转交给其他班组。"""

    from_crew: str = ""
    to_crew: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Paused(Event):
    """暂停（等配件、等天气、等居民配合等）。"""

    reason: str = ""


@dataclass(frozen=True)
class Resumed(Event):
    """从暂停恢复，回到暂停前状态。"""


@dataclass(frozen=True)
class SubmittedForAcceptance(Event):
    """维修完成，提交验收。"""

    summary: str = ""


@dataclass(frozen=True)
class Accepted(Event):
    """验收通过。"""

    opinion: str = ""
    reviewer: str = ""


@dataclass(frozen=True)
class AcceptanceRejected(Event):
    """验收不通过：只能回到整改阶段。"""

    opinion: str = ""
    reviewer: str = ""


@dataclass(frozen=True)
class RectificationStarted(Event):
    """整改阶段开始（验收驳回后系统自动流转，或班组主动进入）。"""

    reason: str = ""


@dataclass(frozen=True)
class Merged(Event):
    """本工单被并入主工单，原始来源保留在主工单来源链中。"""

    primary_id: str = ""
    location: str = ""
    category: str = "other"
    risk: int = 0
    photo_summary: str = ""
    reporter_name: str = ""
    reporter_contact: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class SourceMerged(Event):
    """主工单侧事件：一条原始报修来源并入本工单（来源链可回溯）。"""

    source_id: str = ""
    original_ticket_id: str = ""
    location: str = ""
    category: str = "other"
    risk: int = 0
    photo_summary: str = ""
    reporter_name: str = ""
    reporter_contact: str = ""


@dataclass(frozen=True)
class ClosedDuplicate(Event):
    """重复报修关闭（不并入来源链时使用，例如已验收件再次报修）。"""

    primary_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OverdueMarked(Event):
    """系统标记超时及原因。"""

    reason: str = ""
    overdue_hours: float = 0.0


_EVENT_TYPES: dict[str, type[Event]] = {}


def _register() -> None:
    import sys as _sys

    module = _sys.modules[__name__]
    for _name in (
        "Reported",
        "Supplemented",
        "Dispatched",
        "ProgressPosted",
        "Transferred",
        "Paused",
        "Resumed",
        "SubmittedForAcceptance",
        "Accepted",
        "AcceptanceRejected",
        "RectificationStarted",
        "Merged",
        "SourceMerged",
        "ClosedDuplicate",
        "OverdueMarked",
    ):
        _EVENT_TYPES[_name] = getattr(module, _name)


_register()

# 事件构造公共字段（payload 之外）
_BASE_FIELDS = ("ticket_id", "seq", "actor", "ts", "client_id", "client_seq", "event_id")


def event_from_row(row: Any) -> Event:
    """把持久化行还原为事件对象。"""
    payload = json.loads(row["payload"]) if row["payload"] else {}
    cls = _EVENT_TYPES[row["etype"]]
    valid = {f.name for f in dataclasses.fields(cls)}
    kwargs = {key: row[key] for key in _BASE_FIELDS if key in valid}
    for key, value in payload.items():
        if key in valid:
            kwargs[key] = value
    return cls(**kwargs)  # type: ignore[arg-type]


def make_event(
    etype: str,
    ticket_id: str,
    seq: int,
    actor: str,
    client_id: str | None = None,
    client_seq: int | None = None,
    ts: float | None = None,
    **payload: Any,
) -> Event:
    cls: Callable[..., Event] = _EVENT_TYPES[etype]
    kwargs: dict[str, Any] = {
        "ticket_id": ticket_id,
        "seq": seq,
        "actor": actor,
        "client_id": client_id,
        "client_seq": client_seq,
    }
    if ts is not None:
        kwargs["ts"] = ts
    fields = {f.name for f in dataclasses.fields(cls)}
    for key, value in payload.items():
        if key in fields:
            kwargs[key] = value
    return cls(**kwargs)
