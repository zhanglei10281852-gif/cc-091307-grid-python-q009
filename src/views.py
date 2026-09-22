"""视图层：按岗位权限隐藏联系方式，管理端总览（负荷 / 超时原因 / 验收意见）。"""
from __future__ import annotations

from typing import Any

from .models import (
    ACTIVE_ORDER_STATUSES,
    CONTACT_PERMISSIONS,
    MANAGEMENT_ROLES,
    ContactVisibility,
    OrderStatus,
    Report,
    Role,
)
from .service import RepairService, ServiceError


def mask_phone(phone: str) -> str:
    if len(phone) >= 7:
        return f"{phone[:3]}****{phone[-4:]}"
    return "***"


def mask_name(name: str) -> str:
    if not name:
        return "***"
    return name[0] + "*" * (len(name) - 1)


def contact_view(report: Report, role: Role) -> dict[str, str]:
    """报修人联系方式按岗位权限隐藏。"""
    level = CONTACT_PERMISSIONS.get(role, ContactVisibility.HIDDEN)
    if level == ContactVisibility.FULL:
        return {
            "reporter_name": report.reporter_name,
            "reporter_phone": report.reporter_phone,
        }
    if level == ContactVisibility.MASKED:
        return {
            "reporter_name": mask_name(report.reporter_name),
            "reporter_phone": mask_phone(report.reporter_phone),
        }
    return {"reporter_name": "***", "reporter_phone": "***"}


def report_view(service: RepairService, report_id: str, role: Role) -> dict[str, Any]:
    """报修单视图：设施信息 + 状态 + 按角色脱敏的联系方式。"""
    trace = service.trace_report(report_id)
    report = trace["report"]
    master = trace["master"]
    view = {
        "id": report.id,
        "category": report.category,
        "region": report.region,
        "address": report.address,
        "risk_level": report.risk_level.label,
        "photo_summary": report.photo_summary,
        "status": report.status.value,
        "merged_into": report.merged_into,
        "created_at": report.created_at.isoformat(),
        **contact_view(report, role),
    }
    if master.id != report.id:
        view["master_id"] = master.id
    if trace["sources"]:
        view["merged_sources"] = [s.id for s in trace["sources"]]
    return view


def chain_view(service: RepairService, order_id: str, role: Role) -> dict[str, Any]:
    """处理链视图：工单 + 事件时间线 + 验收意见，联系方式按角色脱敏。"""
    chain = service.get_chain(order_id)
    order = chain["order"]
    report = chain["report"]
    return {
        "order_id": order.id,
        "report_id": report.id,
        "status": order.status.value,
        "crew_id": order.crew_id,
        "priority": order.priority,
        "priority_reason": order.priority_reason,
        "facility": f"{report.region}{report.address}·{report.category}",
        **contact_view(report, role),
        "timeline": [
            {
                "seq": e.seq,
                "type": e.event_type,
                "actor": e.actor,
                "payload": e.payload,
                "at": e.created_at.isoformat(),
            }
            for e in chain["events"]
        ],
        "acceptances": [
            {
                "round": a.round,
                "passed": a.passed,
                "opinion": a.opinion,
                "inspector": a.inspector,
                "at": a.created_at.isoformat(),
            }
            for a in chain["acceptances"]
        ],
    }


def management_overview(service: RepairService, role: Role) -> dict[str, Any]:
    """管理端总览：班组负荷、超时原因、每项验收意见。"""
    if role not in MANAGEMENT_ROLES:
        raise ServiceError(f"角色{role.value}无权查看管理端总览")
    now = service.clock()

    crew_load = []
    for crew in service.store.all_crews():
        active = [
            o for o in service.store.all_orders()
            if o.crew_id == crew.id and o.status in ACTIVE_ORDER_STATUSES
        ]
        by_status: dict[str, int] = {}
        for order in active:
            by_status[order.status.value] = by_status.get(order.status.value, 0) + 1
        crew_load.append({
            "crew_id": crew.id,
            "crew_name": crew.name,
            "capacity": crew.max_parallel,
            "active_orders": len(active),
            "by_status": by_status,
            "overloaded": len(active) > crew.max_parallel,
        })

    overdue = service.overdue_orders(now)
    overdue_by_order = {item["order"].id: item for item in overdue}
    for entry in crew_load:
        entry["overdue_orders"] = sum(
            1
            for o in service.store.all_orders()
            if o.crew_id == entry["crew_id"] and o.id in overdue_by_order
        )

    timeouts = [
        {
            "order_id": item["order"].id,
            "report_id": item["report"].id,
            "facility": (
                f"{item['report'].region}{item['report'].address}"
                f"·{item['report'].category}"
            ),
            "risk_level": item["report"].risk_level.label,
            "status": item["order"].status.value,
            "crew_id": item["order"].crew_id,
            "overdue_hours": item["overdue_hours"],
            "reason": item["reason"],
        }
        for item in overdue
    ]

    orders = {o.id: o for o in service.store.all_orders()}
    reports = {r.id: r for r in service.store.all_reports()}
    acceptance_opinions = []
    for record in service.store.all_acceptances():
        order = orders.get(record.order_id)
        report = reports.get(order.report_id) if order else None
        acceptance_opinions.append({
            "order_id": record.order_id,
            "facility": (
                f"{report.region}{report.address}·{report.category}"
                if report else None
            ),
            "round": record.round,
            "passed": record.passed,
            "opinion": record.opinion,
            "inspector": record.inspector,
            "at": record.created_at.isoformat(),
        })

    pending_unassigned = [
        o.id for o in service.store.all_orders()
        if o.status == OrderStatus.PENDING
    ]
    return {
        "generated_at": now.isoformat(),
        "crew_load": crew_load,
        "pending_unassigned": pending_unassigned,
        "timeouts": timeouts,
        "acceptance_opinions": acceptance_opinions,
    }
