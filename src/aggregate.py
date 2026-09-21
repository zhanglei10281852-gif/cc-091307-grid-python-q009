"""工单聚合：通过重放事件流得到当前状态（事件溯源）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event
from .models import Category, Risk, State

_TERMINAL = {State.ACCEPTED, State.MERGED, State.CLOSED_DUP}
_ACTIVE = {
    State.DISPATCHED,
    State.IN_PROGRESS,
    State.RECTIFYING,
    State.PENDING_ACCEPTANCE,
    State.PAUSED,
}


@dataclass
class Review:
    result: str  # passed / rejected
    opinion: str
    reviewer: str
    ts: float
    seq: int


@dataclass
class MergedSource:
    """并入主工单的原始报修来源（合并后仍可回溯）。"""

    source_id: str
    original_ticket_id: str
    reporter_name: str
    reporter_contact: str
    location: str
    category: str
    risk: int
    photo_summary: str
    ts: float
    seq: int


@dataclass
class Ticket:
    ticket_id: str
    version: int = 0
    state: State = State.PENDING

    location: str = ""
    region: str = ""
    category: str = Category.OTHER.value
    risk: int = Risk.UNKNOWN.value
    photo_summary: str = ""
    reporter_name: str = ""
    reporter_contact: str = ""
    source_id: str = ""

    crew_id: str | None = None
    crew_history: list[dict[str, Any]] = field(default_factory=list)

    created_at: float | None = None
    dispatched_at: float | None = None
    last_progress_at: float | None = None
    last_progress_text: str = ""
    submitted_at: float | None = None
    submitted_summary: str = ""
    accepted_at: float | None = None

    paused_from: State | None = None
    pause_reason: str = ""
    paused_at: float | None = None
    paused_total: float = 0.0  # 累计暂停时长（秒），不计入 SLA 工时

    merged_into: str | None = None
    merged_sources: list[MergedSource] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)

    overdue: dict[str, Any] | None = None
    latest_seq_by_type: dict[str, int] = field(default_factory=dict)

    @property
    def category_enum(self) -> Category:
        return Category(self.category)

    @property
    def risk_enum(self) -> Risk:
        return Risk(self.risk)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def is_active(self) -> bool:
        return self.state in _ACTIVE

    @property
    def is_paused(self) -> bool:
        return self.state is State.PAUSED


def build_ticket(events: list[Event]) -> Ticket:
    """按序号重放事件，还原工单聚合。"""
    if not events:
        raise ValueError("empty event stream")
    first = events[0]
    t = Ticket(ticket_id=first.ticket_id)

    for e in events:
        t.version = e.seq
        t.latest_seq_by_type[e.etype] = e.seq
        etype = e.etype
        p = e.payload()

        if etype == "Reported":
            t.state = State.PENDING
            t.location = p["location"]
            t.region = p["region"]
            t.category = p["category"]
            t.risk = p["risk"]
            t.photo_summary = p["photo_summary"]
            t.reporter_name = p["reporter_name"]
            t.reporter_contact = p["reporter_contact"]
            t.source_id = p["source_id"]
            t.created_at = e.ts

        elif etype == "Supplemented":
            if p.get("photo_summary"):
                t.photo_summary = p["photo_summary"]
            if p.get("reporter_contact"):
                t.reporter_contact = p["reporter_contact"]
            if p.get("risk") is not None:
                t.risk = max(t.risk, p["risk"])

        elif etype == "Dispatched":
            t.state = State.DISPATCHED
            t.crew_id = p["crew_id"]
            t.region = p.get("region") or t.region
            if p.get("risk") is not None:
                t.risk = max(t.risk, p["risk"])
            t.dispatched_at = e.ts
            t.crew_history.append(
                {"crew_id": p["crew_id"], "ts": e.ts, "via": "dispatch", "reason": p.get("reason", "")}
            )

        elif etype == "ProgressPosted":
            phase = p.get("phase") or ""
            phase_state = {
                "dispatched": State.DISPATCHED,
                "in_progress": State.IN_PROGRESS,
                "rectifying": State.RECTIFYING,
            }.get(phase)
            if phase_state is not None and t.state not in _TERMINAL:
                t.state = phase_state
            t.last_progress_at = e.ts
            t.last_progress_text = p.get("status_text", "")

        elif etype == "Transferred":
            t.crew_id = p["to_crew"]
            # 转交不改变处理阶段：已整改中的仍回到整改，不能借机跳过整改
            if t.state not in (State.DISPATCHED, State.IN_PROGRESS, State.RECTIFYING):
                t.state = State.DISPATCHED
            t.crew_history.append(
                {
                    "crew_id": p["to_crew"],
                    "ts": e.ts,
                    "via": "transfer",
                    "from_crew": p.get("from_crew", ""),
                    "reason": p.get("reason", ""),
                }
            )

        elif etype == "Paused":
            if t.state != State.PAUSED:
                t.paused_from = t.state
            t.state = State.PAUSED
            t.pause_reason = p.get("reason", "")
            t.paused_at = e.ts

        elif etype == "Resumed":
            t.state = t.paused_from or State.IN_PROGRESS
            if t.paused_at is not None:
                t.paused_total += max(0.0, e.ts - t.paused_at)
            t.paused_at = None
            t.pause_reason = ""
            t.paused_from = None

        elif etype == "SubmittedForAcceptance":
            t.state = State.PENDING_ACCEPTANCE
            t.submitted_at = e.ts
            t.submitted_summary = p.get("summary", "")

        elif etype == "Accepted":
            t.state = State.ACCEPTED
            t.accepted_at = e.ts
            t.reviews.append(
                Review("passed", p.get("opinion", ""), p.get("reviewer", e.actor), e.ts, e.seq)
            )

        elif etype == "AcceptanceRejected":
            # 验收不通过：只能回到整改阶段
            t.state = State.RECTIFYING
            t.reviews.append(
                Review("rejected", p.get("opinion", ""), p.get("reviewer", e.actor), e.ts, e.seq)
            )

        elif etype == "RectificationStarted":
            t.state = State.RECTIFYING

        elif etype == "Merged":
            t.state = State.MERGED
            t.merged_into = p["primary_id"]

        elif etype == "SourceMerged":
            # 重复报修若暴露了更高风险，主件风险等级随之提升（优先级依据）
            t.risk = max(t.risk, int(p.get("risk") or 0))
            t.merged_sources.append(
                MergedSource(
                    source_id=p.get("source_id", ""),
                    original_ticket_id=p.get("original_ticket_id", ""),
                    reporter_name=p.get("reporter_name", ""),
                    reporter_contact=p.get("reporter_contact", ""),
                    location=p.get("location", ""),
                    category=p.get("category", t.category),
                    risk=p.get("risk", 0),
                    photo_summary=p.get("photo_summary", ""),
                    ts=e.ts,
                    seq=e.seq,
                )
            )

        elif etype == "ClosedDuplicate":
            t.state = State.CLOSED_DUP
            t.merged_into = p.get("primary_id")

        elif etype == "OverdueMarked":
            t.overdue = {
                "reason": p.get("reason", ""),
                "overdue_hours": p.get("overdue_hours", 0.0),
                "ts": e.ts,
                "seq": e.seq,
            }

    return t
