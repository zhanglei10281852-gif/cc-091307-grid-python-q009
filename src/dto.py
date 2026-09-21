"""只读序列化：把聚合投影成对外 DTO，按岗位身份脱敏联系方式。"""
from __future__ import annotations

from typing import Any

from .aggregate import Ticket, build_ticket
from .models import SLA_HOURS
from .security import Identity, render_contact
from .storage import EventStore


def ticket_to_dict(
    ticket: Ticket,
    identity: Identity,
    *,
    now: float | None = None,
    include_sources: bool = True,
) -> dict[str, Any]:
    contact = render_contact(identity, ticket.crew_id, ticket.reporter_contact)

    sla = SLA_HOURS.get((ticket.category_enum, ticket.risk_enum))
    elapsed = None
    overdue = None
    if ticket.dispatched_at is not None and not ticket.is_terminal:
        end = now or 0.0
        paused = ticket.paused_total
        if ticket.is_paused and ticket.paused_at is not None:
            paused += max(0.0, end - ticket.paused_at)
        elapsed = max(0.0, end - ticket.dispatched_at - paused) / 3600.0
        if sla is not None and elapsed > sla:
            reasons = [f"超过 {sla:g} 小时 SLA（已用时 {elapsed:.1f} 小时）"]
            if ticket.is_paused and ticket.pause_reason:
                reasons.append(f"暂停中：{ticket.pause_reason}")
            if ticket.state.value == "rectifying":
                rejected = sum(1 for r in ticket.reviews if r.result == "rejected")
                reasons.append(f"验收驳回后整改（第 {max(1, rejected)} 轮）")
            overdue = {"sla_hours": sla, "elapsed_hours": round(elapsed, 2),
                       "reason": "；".join(reasons)}

    data: dict[str, Any] = {
        "ticket_id": ticket.ticket_id,
        "version": ticket.version,
        "state": ticket.state.value,
        "location": ticket.location,
        "region": ticket.region,
        "category": ticket.category,
        "risk": ticket.risk,
        "photo_summary": ticket.photo_summary,
        "reporter_name": ticket.reporter_name,
        "reporter_contact": contact,
        "contact_visibility": (
            "hidden" if contact is None else ("masked" if contact != ticket.reporter_contact else "full")
        ),
        "source_id": ticket.source_id,
        "crew_id": ticket.crew_id,
        "crew_history": ticket.crew_history,
        "created_at": ticket.created_at,
        "dispatched_at": ticket.dispatched_at,
        "last_progress_at": ticket.last_progress_at,
        "last_progress_text": ticket.last_progress_text,
        "submitted_at": ticket.submitted_at,
        "submitted_summary": ticket.submitted_summary,
        "accepted_at": ticket.accepted_at,
        "pause_reason": ticket.pause_reason,
        "paused_at": ticket.paused_at,
        "merged_into": ticket.merged_into,
        "sla_hours": sla,
        "elapsed_hours": round(elapsed, 2) if elapsed is not None else None,
        "overdue": overdue,
        "recorded_overdue": ticket.overdue,
        "reviews": [
            {
                "result": r.result,
                "opinion": r.opinion,
                "reviewer": r.reviewer,
                "ts": r.ts,
                "seq": r.seq,
            }
            for r in ticket.reviews
        ],
    }
    if include_sources:
        data["merged_sources"] = [
            {
                "source_id": s.source_id,
                "original_ticket_id": s.original_ticket_id,
                "location": s.location,
                "category": s.category,
                "risk": s.risk,
                "photo_summary": s.photo_summary,
                "reporter_name": s.reporter_name,
                "reporter_contact": render_contact(identity, ticket.crew_id, s.reporter_contact),
                "merged_at": s.ts,
            }
            for s in ticket.merged_sources
        ]
    return data


def event_to_dict(event: Any, identity: Identity, ticket: Ticket | None = None) -> dict[str, Any]:
    payload = event.payload()
    crew_id = ticket.crew_id if ticket else None
    for key in ("reporter_contact",):
        if key in payload and payload[key]:
            payload[key] = render_contact(identity, crew_id, payload[key]) or "***"
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "type": event.etype,
        "actor": event.actor,
        "ts": event.ts,
        "client_id": event.client_id,
        "client_seq": event.client_seq,
        "payload": payload,
    }


def load_ticket(store: EventStore, ticket_id: str) -> Ticket | None:
    events = store.load_events(ticket_id)
    if not events:
        return None
    return build_ticket(events)
