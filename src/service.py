"""设施报修统筹核心服务：受理、派工、处理链状态机、幂等事件与合并回溯。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import (
    ACTIVE_ORDER_STATUSES,
    CREW_EVENT_TRANSITIONS,
    PRIORITY_WEIGHT,
    SLA_HOURS,
    AcceptanceRecord,
    Crew,
    Event,
    EventType,
    OrderStatus,
    Report,
    ReportStatus,
    RiskLevel,
    WorkOrder,
)
from .storage import SQLiteStorage


class ServiceError(Exception):
    """服务层错误基类。"""


class NotFoundError(ServiceError):
    """报修单 / 工单 / 班组不存在。"""


class InvalidTransitionError(ServiceError):
    """当前状态不允许该操作。"""


class StaleEventError(ServiceError):
    """过期更新：序号已被其他事件占用。"""


class SeqGapError(ServiceError):
    """序号跳跃：客户端缺少中间事件，需先同步。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _canonical(payload: Optional[dict]) -> dict:
    """JSON 归一化，保证重复事件比对的稳定性。"""
    return json.loads(json.dumps(payload or {}, ensure_ascii=False, sort_keys=True))


class RepairService:
    """设施报修统筹服务。

    - 受理报修并按风险等级与区域自动匹配班组，生成可追踪处理链；
    - 报修可补充、合并、转交、暂停、验收，合并后来源单可回溯；
    - 班组移动端事件按序号幂等处理，重复提交返回已记录结果，过期更新被拒绝；
    - 所有状态与事件持久化于 SQLite，进程重启后完整保留。
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.store = SQLiteStorage(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 班组
    # ------------------------------------------------------------------

    def register_crew(
        self,
        name: str,
        categories: list[str],
        regions: list[str],
        max_parallel: int = 5,
    ) -> Crew:
        crew = Crew(
            id=_new_id("CRW"), name=name,
            categories=tuple(categories), regions=tuple(regions),
            max_parallel=max_parallel,
        )
        with self.store.tx():
            self.store.insert_crew(crew)
        return crew

    def crew_load(self, crew_id: str) -> int:
        """班组当前负荷：仍在处理链上的工单数。"""
        return sum(
            1
            for o in self.store.all_orders()
            if o.crew_id == crew_id and o.status in ACTIVE_ORDER_STATUSES
        )

    # ------------------------------------------------------------------
    # 报修受理与派工
    # ------------------------------------------------------------------

    def submit_report(
        self,
        *,
        category: str,
        region: str,
        address: str,
        risk_level: RiskLevel,
        photo_summary: str,
        reporter_name: str,
        reporter_phone: str,
        actor: str = "grid-center",
        auto_merge: bool = True,
    ) -> Report:
        """受理报修：同一设施已有处理中报修时自动合并，否则自动派工。"""
        now = self.clock()
        report = Report(
            id=_new_id("RPT"), category=category, region=region, address=address,
            risk_level=risk_level, photo_summary=photo_summary,
            reporter_name=reporter_name, reporter_phone=reporter_phone,
            status=ReportStatus.PENDING, created_at=now, updated_at=now,
        )
        with self.store.tx():
            # 先查重再插入，避免新单匹配到自身
            master = (
                self.store.find_open_report(report.facility_key)
                if auto_merge else None
            )
            self.store.insert_report(report)
            self._append_report_event(
                report.id, EventType.REPORT_CREATED, actor,
                {
                    "category": category, "region": region, "address": address,
                    "risk_level": risk_level.label, "photo_summary": photo_summary,
                },
            )
            if master is not None:
                self._merge_locked(
                    master.id, [report.id],
                    reason="同一设施重复报修，自动合并", actor=actor,
                )
            else:
                self._dispatch_locked(report, actor)
        return self.store.get_report(report.id)

    def _dispatch_locked(
        self, report: Report, actor: str, order: Optional[WorkOrder] = None
    ) -> WorkOrder:
        """按风险与区域匹配班组并生成处理链；无匹配时工单挂起待人工派工。"""
        now = self.clock()
        weight = PRIORITY_WEIGHT[report.risk_level]
        if order is None:
            order = WorkOrder(
                id=_new_id("WO"), report_id=report.id, crew_id=None,
                status=OrderStatus.PENDING, priority=weight, priority_reason="",
                last_seq=0, created_at=now, updated_at=now,
            )
            is_new = True
        else:
            order.priority = weight
            order.updated_at = now
            is_new = False
        eligible = [
            c for c in self.store.all_crews()
            if report.category in c.categories and report.region in c.regions
        ]
        if eligible:
            chosen = min(eligible, key=lambda c: (self.crew_load(c.id), c.name))
            load = self.crew_load(chosen.id)
            order.crew_id = chosen.id
            order.status = OrderStatus.DISPATCHED
            order.priority_reason = (
                f"风险等级={report.risk_level.label}（权重{weight}），"
                f"按区域'{report.region}'+类别'{report.category}'匹配班组"
                f"'{chosen.name}'，该班组当前负荷{load}/{chosen.max_parallel}单"
            )
            if load >= chosen.max_parallel:
                order.priority_reason += "，已超额定负荷"
            report.status = ReportStatus.ACTIVE
            report.updated_at = now
            self.store.update_report(report)
            if is_new:
                self.store.insert_order(order)
            self._append_order_event(
                order, EventType.DISPATCHED, actor,
                {
                    "crew_id": chosen.id, "crew_name": chosen.name,
                    "priority": weight, "priority_reason": order.priority_reason,
                    "sla_hours": SLA_HOURS[report.risk_level],
                },
            )
        else:
            order.priority_reason = "无匹配班组，待人工派工"
            if is_new:
                self.store.insert_order(order)
            self._append_order_event(
                order, EventType.DISPATCH_FAILED, actor,
                {
                    "reason": f"没有班组同时覆盖类别'{report.category}'"
                              f"与区域'{report.region}'",
                },
            )
        self.store.update_order(order)
        return order

    def redispatch(self, order_id: str, actor: str = "dispatcher") -> WorkOrder:
        """对挂起的工单重新自动匹配班组。"""
        with self.store.tx():
            order = self._get_order(order_id)
            if order.status != OrderStatus.PENDING:
                raise InvalidTransitionError(
                    f"工单{order_id}当前为{order.status.value}，无需重新派工"
                )
            report = self._get_report(order.report_id)
            self._dispatch_locked(report, actor, order=order)
        return self.store.get_order(order_id)

    # ------------------------------------------------------------------
    # 补充 / 合并
    # ------------------------------------------------------------------

    def supplement_report(
        self,
        report_id: str,
        *,
        actor: str,
        note: Optional[str] = None,
        photo_summary: Optional[str] = None,
        risk_level: Optional[RiskLevel] = None,
    ) -> Report:
        """补充报修：追加说明/照片摘要，可调整风险等级；已合并单落到主单。"""
        with self.store.tx():
            report = self._ultimate_master(self._get_report(report_id))
            if report.status == ReportStatus.RESOLVED:
                raise InvalidTransitionError("已完成的报修不能补充")
            payload: dict[str, Any] = {}
            if report.id != report_id:
                payload["supplemented_for"] = report_id
            if note:
                payload["note"] = note
            now = self.clock()
            if photo_summary:
                payload["photo_summary"] = photo_summary
                report.photo_summary += f"\n[补充] {photo_summary}"
            if risk_level is not None and risk_level != report.risk_level:
                payload["risk_change"] = (
                    f"{report.risk_level.label}→{risk_level.label}"
                )
                report.risk_level = risk_level
                order = self.store.active_order_for_report(report.id)
                if order is not None:
                    order.priority = PRIORITY_WEIGHT[risk_level]
                    order.updated_at = now
                    self._append_order_event(
                        order, EventType.PRIORITY_CHANGED, actor,
                        {
                            "new_priority": order.priority,
                            "reason": "补充报修调整风险等级为"
                                      f"{risk_level.label}",
                        },
                    )
                    self.store.update_order(order)
            report.updated_at = now
            self.store.update_report(report)
            self._append_report_event(
                report.id, EventType.SUPPLEMENTED, actor, payload
            )
        return self.store.get_report(report.id)

    def merge_reports(
        self,
        master_id: str,
        source_ids: list[str],
        *,
        reason: str,
        actor: str = "dispatcher",
    ) -> Report:
        """合并报修：来源单保留并指向主单，来源工单关闭，全程留痕可回溯。"""
        with self.store.tx():
            master = self._merge_locked(master_id, source_ids, reason, actor)
        return master

    def _merge_locked(
        self, master_id: str, source_ids: list[str], reason: str, actor: str
    ) -> Report:
        master = self._ultimate_master(self._get_report(master_id))
        if master.status == ReportStatus.RESOLVED:
            raise InvalidTransitionError("已完成的报修不能作为合并主单")
        master_order = self.store.active_order_for_report(master.id)
        now = self.clock()
        for source_id in source_ids:
            source = self._get_report(source_id)
            if source.id == master.id:
                raise ServiceError("不能将报修单合并到自身")
            if source.status == ReportStatus.MERGED:
                raise InvalidTransitionError(
                    f"报修单{source_id}已合并至{source.merged_into}"
                )
            if source.status == ReportStatus.RESOLVED:
                raise InvalidTransitionError(f"报修单{source_id}已完成，不能合并")
            source.status = ReportStatus.MERGED
            source.merged_into = master.id
            source.updated_at = now
            self.store.update_report(source)
            self._append_report_event(
                source.id, EventType.MERGED, actor,
                {"merged_into": master.id, "reason": reason},
            )
            self._append_report_event(
                master.id, EventType.ABSORBED_SOURCE, actor,
                {"source_report": source.id, "reason": reason},
            )
            source_order = self.store.active_order_for_report(source.id)
            if source_order is not None:
                source_order.status = OrderStatus.CANCELLED
                source_order.updated_at = now
                self._append_order_event(
                    source_order, EventType.MERGE_CLOSED, actor,
                    {
                        "merged_into_report": master.id,
                        "merged_into_order": (
                            master_order.id if master_order else None
                        ),
                        "reason": reason,
                    },
                )
                self.store.update_order(source_order)
                if master_order is not None:
                    self._append_order_event(
                        master_order, EventType.MERGED_SOURCE, actor,
                        {
                            "source_report": source.id,
                            "source_order": source_order.id,
                        },
                    )
                    self.store.update_order(master_order)
        return self.store.get_report(master.id)

    def trace_report(self, report_id: str) -> dict:
        """合并回溯：任一来源单都能还原主单、全部来源及各自事件。"""
        report = self._get_report(report_id)
        master = self._ultimate_master(report)
        return {
            "report": report,
            "master": master,
            "sources": self.store.reports_merged_into(master.id),
            "report_events": self.store.events_for("report", report.id),
            "master_events": self.store.events_for("report", master.id),
        }

    # ------------------------------------------------------------------
    # 转交 / 暂停 / 恢复
    # ------------------------------------------------------------------

    def transfer_order(
        self,
        order_id: str,
        to_crew_id: str,
        *,
        reason: str,
        actor: str = "dispatcher",
    ) -> WorkOrder:
        """转交工单给同区域同类别的其他班组。"""
        with self.store.tx():
            order = self._get_order(order_id)
            if order.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
                raise InvalidTransitionError(
                    f"工单已{order.status.value}，不能转交"
                )
            if order.crew_id == to_crew_id:
                raise ServiceError("工单已在该班组名下")
            crew = self.store.get_crew(to_crew_id)
            if crew is None:
                raise NotFoundError(f"班组不存在: {to_crew_id}")
            report = self._get_report(order.report_id)
            if report.category not in crew.categories or report.region not in crew.regions:
                raise ServiceError(
                    f"班组'{crew.name}'不覆盖类别'{report.category}'"
                    f"或区域'{report.region}'，不能转交"
                )
            from_crew = order.crew_id
            order.crew_id = to_crew_id
            order.updated_at = self.clock()
            self._append_order_event(
                order, EventType.TRANSFERRED, actor,
                {"from_crew": from_crew, "to_crew": to_crew_id, "reason": reason},
            )
            self.store.update_order(order)
        return self.store.get_order(order_id)

    def pause_order(
        self, order_id: str, *, reason: str, actor: str = "dispatcher"
    ) -> WorkOrder:
        with self.store.tx():
            order = self._get_order(order_id)
            if order.status not in (OrderStatus.DISPATCHED, OrderStatus.IN_PROGRESS):
                raise InvalidTransitionError(
                    f"工单状态为{order.status.value}，不能暂停"
                )
            order.paused_from = order.status
            order.status = OrderStatus.PAUSED
            order.updated_at = self.clock()
            self._append_order_event(
                order, EventType.PAUSED, actor, {"reason": reason}
            )
            self.store.update_order(order)
        return self.store.get_order(order_id)

    def resume_order(
        self, order_id: str, *, actor: str = "dispatcher"
    ) -> WorkOrder:
        with self.store.tx():
            order = self._get_order(order_id)
            if order.status != OrderStatus.PAUSED:
                raise InvalidTransitionError("工单未处于暂停状态")
            order.status = order.paused_from or OrderStatus.DISPATCHED
            order.paused_from = None
            order.updated_at = self.clock()
            self._append_order_event(order, EventType.RESUMED, actor, {})
            self.store.update_order(order)
        return self.store.get_order(order_id)

    # ------------------------------------------------------------------
    # 班组移动端事件：按序号幂等，拒绝过期更新
    # ------------------------------------------------------------------

    def apply_crew_event(
        self,
        order_id: str,
        seq: int,
        event_type: str,
        payload: Optional[dict] = None,
        *,
        actor: str,
    ) -> dict:
        """应用班组移动端事件。

        - seq == last_seq + 1：正常应用；
        - seq <= last_seq 且内容一致：重复发送，幂等返回已记录结果；
        - seq <= last_seq 但内容不一致：过期更新，拒绝；
        - seq > last_seq + 1：跳号，拒绝并要求客户端先同步。
        """
        payload = _canonical(payload)
        with self.store.tx():
            order = self._get_order(order_id)
            if seq > order.last_seq + 1:
                raise SeqGapError(
                    f"序号{seq}跳跃：当前已处理到{order.last_seq}，请先同步"
                )
            if seq <= order.last_seq:
                recorded = self.store.get_event("order", order_id, seq)
                if (
                    recorded is not None
                    and recorded.event_type == event_type
                    and recorded.actor == actor
                    and recorded.payload == payload
                ):
                    result = dict(recorded.result or {})
                    result["duplicate"] = True
                    return result
                raise StaleEventError(
                    f"序号{seq}的更新已过期，与已记录事件冲突，已拒绝"
                )
            transitions = CREW_EVENT_TRANSITIONS.get(event_type)
            if transitions is None:
                raise InvalidTransitionError(f"班组不能上报事件: {event_type}")
            if order.status not in transitions:
                raise InvalidTransitionError(
                    f"工单状态为{order.status.value}，不能执行{event_type}"
                )
            order.status = transitions[order.status]
            order.updated_at = self.clock()
            result = {
                "order_id": order.id,
                "seq": seq,
                "status": order.status.value,
                "duplicate": False,
            }
            self._append_order_event(order, event_type, actor, payload, result)
            self.store.update_order(order)
            return dict(result)

    def next_crew_seq(self, order_id: str) -> int:
        """班组客户端应使用的下一个事件序号。

        客户端联网时以此为准；离线缓存的事件重连上报时若序号已被
        服务端事件（暂停/转交/验收等）占用，将按过期更新拒绝。
        """
        return self._get_order(order_id).last_seq + 1

    # ------------------------------------------------------------------
    # 验收：不通过只能回到整改阶段
    # ------------------------------------------------------------------

    def submit_acceptance(
        self,
        order_id: str,
        *,
        passed: bool,
        opinion: str,
        inspector: str,
        actor: Optional[str] = None,
    ) -> WorkOrder:
        """验收：通过则完成；不通过只能回到整改阶段。"""
        with self.store.tx():
            order = self._get_order(order_id)
            if order.status != OrderStatus.PENDING_ACCEPTANCE:
                raise InvalidTransitionError(
                    f"工单状态为{order.status.value}，不能验收"
                )
            now = self.clock()
            records = self.store.acceptances_for_order(order_id)
            self.store.insert_acceptance(
                AcceptanceRecord(
                    id=0, order_id=order_id, round=len(records) + 1,
                    passed=passed, opinion=opinion, inspector=inspector,
                    created_at=now,
                )
            )
            if passed:
                order.status = OrderStatus.COMPLETED
                order.completed_at = now
                self._append_order_event(
                    order, EventType.ACCEPTED, actor or inspector,
                    {"opinion": opinion, "inspector": inspector},
                )
                report = self._get_report(order.report_id)
                report.status = ReportStatus.RESOLVED
                report.updated_at = now
                self.store.update_report(report)
                self._append_report_event(
                    report.id, EventType.REPORT_RESOLVED, actor or inspector,
                    {"order_id": order.id},
                )
            else:
                # 验收不通过：唯一去向是整改阶段
                order.status = OrderStatus.IN_PROGRESS
                self._append_order_event(
                    order, EventType.ACCEPTANCE_FAILED, actor or inspector,
                    {"opinion": opinion, "inspector": inspector},
                )
            order.updated_at = now
            self.store.update_order(order)
        return self.store.get_order(order_id)

    # ------------------------------------------------------------------
    # 查询：处理链 / 超时
    # ------------------------------------------------------------------

    def get_chain(self, order_id: str) -> dict:
        """可追踪处理链：工单快照 + 全部事件 + 验收记录。"""
        order = self._get_order(order_id)
        return {
            "order": order,
            "report": self._get_report(order.report_id),
            "events": self.store.events_for("order", order_id),
            "acceptances": self.store.acceptances_for_order(order_id),
        }

    def overdue_orders(self, now: Optional[datetime] = None) -> list[dict]:
        """超时工单及原因（供管理端展示）。"""
        now = now or self.clock()
        tracked = tuple(ACTIVE_ORDER_STATUSES) + (OrderStatus.PENDING,)
        overdue = []
        for order in self.store.all_orders():
            if order.status not in tracked:
                continue
            report = self._get_report(order.report_id)
            sla = SLA_HOURS[report.risk_level]
            elapsed = (now - order.created_at).total_seconds() / 3600
            if elapsed <= sla:
                continue
            overdue.append({
                "order": order,
                "report": report,
                "overdue_hours": round(elapsed - sla, 1),
                "reason": self._timeout_reason(order, report, sla),
            })
        return overdue

    def _timeout_reason(self, order: WorkOrder, report: Report, sla: int) -> str:
        base = f"超出{report.risk_level.label}级处置时限{sla}小时"
        events = self.store.events_for("order", order.id)
        if order.status == OrderStatus.PENDING:
            return f"{base}：无匹配班组，尚未派工"
        if order.status == OrderStatus.PAUSED:
            pause = next(
                (e for e in reversed(events) if e.event_type == EventType.PAUSED),
                None,
            )
            reason = pause.payload.get("reason", "未说明") if pause else "未说明"
            return f"{base}：暂停超时（{reason}）"
        if order.status == OrderStatus.DISPATCHED:
            return f"{base}：班组未接单"
        if order.status == OrderStatus.PENDING_ACCEPTANCE:
            return f"{base}：已提交验收，等待验收结论"
        notes = [
            e for e in events if e.event_type == EventType.PROGRESS_NOTE
        ]
        if notes:
            note = notes[-1].payload.get("note", "")
            return f"{base}：整改超时，最近进度“{note}”"
        return f"{base}：班组已接单但未反馈进度"

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get_report(self, report_id: str) -> Report:
        report = self.store.get_report(report_id)
        if report is None:
            raise NotFoundError(f"报修单不存在: {report_id}")
        return report

    def _get_order(self, order_id: str) -> WorkOrder:
        order = self.store.get_order(order_id)
        if order is None:
            raise NotFoundError(f"工单不存在: {order_id}")
        return order

    def _ultimate_master(self, report: Report) -> Report:
        """沿合并链找到最终主单。"""
        while report.merged_into:
            report = self._get_report(report.merged_into)
        return report

    def _append_report_event(
        self, report_id: str, event_type: str, actor: str, payload: dict
    ) -> None:
        seq = self.store.max_seq("report", report_id) + 1
        self.store.append_event(Event(
            id=0, aggregate_type="report", aggregate_id=report_id, seq=seq,
            event_type=event_type, actor=actor, payload=_canonical(payload),
            result=None, created_at=self.clock(),
        ))

    def _append_order_event(
        self,
        order: WorkOrder,
        event_type: str,
        actor: str,
        payload: dict,
        result: Optional[dict] = None,
    ) -> None:
        """工单事件：占用下一个序号并同步推进 last_seq。"""
        seq = order.last_seq + 1
        self.store.append_event(Event(
            id=0, aggregate_type="order", aggregate_id=order.id, seq=seq,
            event_type=event_type, actor=actor, payload=_canonical(payload),
            result=_canonical(result) if result is not None else None,
            created_at=self.clock(),
        ))
        order.last_seq = seq


#: 兼容包入口约定。
Service = RepairService
