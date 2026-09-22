"""设施报修统筹服务测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models import OrderStatus, ReportStatus, RiskLevel, Role
from src.service import (
    InvalidTransitionError,
    RepairService,
    SeqGapError,
    ServiceError,
    StaleEventError,
)
from src.views import management_overview, report_view


class ManualClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


START = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def svc(tmp_path, clock):
    service = RepairService(str(tmp_path / "test.db"), clock=clock)
    yield service
    service.close()


@pytest.fixture
def crews(svc):
    svc.register_crew("维修一班", ["路灯", "健身器材"], ["幸福里", "滨河"])
    svc.register_crew("维修二班", ["楼道扶手"], ["幸福里"])
    svc.register_crew("综合班", ["路灯", "健身器材", "楼道扶手"], ["幸福里", "滨河"])
    return svc


def submit(svc, **kw):
    defaults = dict(
        category="路灯", region="幸福里", address="东门",
        risk_level=RiskLevel.URGENT, photo_summary="照片：灯灭",
        reporter_name="王建国", reporter_phone="13812345678",
    )
    defaults.update(kw)
    return svc.submit_report(**defaults)


def order_of(svc, report):
    return svc.store.active_order_for_report(report.id)


def crew(svc, order_id, event_type, payload=None, actor="一班"):
    """班组客户端按当前序号上报事件（联网同步后的正常流程）。"""
    seq = svc.next_crew_seq(order_id)
    return svc.apply_crew_event(order_id, seq, event_type, payload or {},
                                actor=actor)


# ----------------------------------------------------------------------
# 受理与派工
# ----------------------------------------------------------------------

class TestDispatch:
    def test_auto_dispatch_matches_region_and_category(self, crews):
        report = submit(crews)
        order = order_of(crews, report)
        assert order.status == OrderStatus.DISPATCHED
        crew = crews.store.get_crew(order.crew_id)
        assert "路灯" in crew.categories and "幸福里" in crew.regions
        assert report.status == ReportStatus.ACTIVE

    def test_priority_reason_records_risk_and_load(self, crews):
        order = order_of(crews, submit(crews))
        assert "风险等级=紧急" in order.priority_reason
        assert "权重100" in order.priority_reason
        assert "负荷" in order.priority_reason

    def test_urgent_gets_higher_priority_than_low(self, crews):
        urgent = order_of(crews, submit(crews, address="北门"))
        low = order_of(crews, submit(crews, address="西门",
                                     risk_level=RiskLevel.LOW))
        assert urgent.priority > low.priority

    def test_least_loaded_crew_is_chosen(self, crews):
        # 维修一班与综合班都覆盖路灯+幸福里，两单应分给不同班组
        o1 = order_of(crews, submit(crews, address="东门"))
        o2 = order_of(crews, submit(crews, address="南门"))
        assert o1.crew_id != o2.crew_id

    def test_no_eligible_crew_leaves_pending(self, svc):
        svc.register_crew("仅路灯班", ["路灯"], ["幸福里"])
        report = submit(svc, category="楼道扶手")
        order = order_of(svc, report)
        assert order.status == OrderStatus.PENDING
        assert order.crew_id is None
        events = svc.get_chain(order.id)["events"]
        assert events[-1].event_type == "DISPATCH_FAILED"

    def test_redispatch_after_registering_crew(self, svc):
        svc.register_crew("仅路灯班", ["路灯"], ["幸福里"])
        report = submit(svc, category="楼道扶手")
        svc.register_crew("扶手班", ["楼道扶手"], ["幸福里"])
        order = svc.redispatch(order_of(svc, report).id)
        assert order.status == OrderStatus.DISPATCHED


# ----------------------------------------------------------------------
# 合并与回溯
# ----------------------------------------------------------------------

class TestMerge:
    def test_duplicate_facility_auto_merged(self, crews):
        first = submit(crews)
        dup = submit(crews, reporter_name="刘芳", reporter_phone="13655556666")
        assert dup.status == ReportStatus.MERGED
        assert dup.merged_into == first.id
        # 同一设施只保留一条处理链，避免重复派工
        assert order_of(crews, dup) is None
        assert order_of(crews, first) is not None

    def test_merge_traceability(self, crews):
        master = submit(crews)
        dup = submit(crews)
        trace = crews.trace_report(dup.id)
        assert trace["master"].id == master.id
        assert [s.id for s in trace["sources"]] == [dup.id]
        # 来源单自身事件完整保留
        types = [e.event_type for e in trace["report_events"]]
        assert "REPORT_CREATED" in types and "MERGED" in types

    def test_manual_merge_closes_source_order(self, crews):
        a = submit(crews, address="东门")
        b = submit(crews, address="东门", auto_merge=False)
        source_order = order_of(crews, b)
        crews.merge_reports(a.id, [b.id], reason="同一设施重复报修")
        assert crews.store.get_order(source_order.id).status == OrderStatus.CANCELLED
        assert crews.store.get_report(b.id).merged_into == a.id
        # 主单工单记录了吸收来源
        master_events = crews.store.events_for("order", order_of(crews, a).id)
        assert any(e.event_type == "MERGED_SOURCE" for e in master_events)

    def test_cannot_merge_invalid(self, crews):
        a = submit(crews)
        b = submit(crews, address="北门")
        crews.merge_reports(a.id, [b.id], reason="重复")
        with pytest.raises(ServiceError):
            crews.merge_reports(a.id, [a.id], reason="自身")
        with pytest.raises(InvalidTransitionError):
            crews.merge_reports(a.id, [b.id], reason="重复合并")

    def test_supplement_to_merged_report_goes_to_master(self, crews):
        master = submit(crews)
        dup = submit(crews)
        updated = crews.supplement_report(dup.id, actor="网格员", note="补充说明")
        assert updated.id == master.id
        events = crews.store.events_for("report", master.id)
        sup = [e for e in events if e.event_type == "SUPPLEMENTED"]
        assert sup and sup[0].payload["supplemented_for"] == dup.id

    def test_supplement_risk_escalation_updates_priority(self, crews):
        report = submit(crews, risk_level=RiskLevel.LOW)
        order = order_of(crews, report)
        old_priority = order.priority
        crews.supplement_report(report.id, actor="网格员",
                                risk_level=RiskLevel.URGENT)
        order = crews.store.get_order(order.id)
        assert order.priority > old_priority
        types = [e.event_type for e in crews.store.events_for("order", order.id)]
        assert "PRIORITY_CHANGED" in types


# ----------------------------------------------------------------------
# 班组移动端事件：幂等与过期拒绝
# ----------------------------------------------------------------------

class TestCrewEvents:
    def test_sequential_events_advance_state(self, crews):
        order = order_of(crews, submit(crews))
        r = crew(crews, order.id, "START_WORK")
        assert r["status"] == OrderStatus.IN_PROGRESS.value
        r = crew(crews, order.id, "PROGRESS_NOTE", {"note": "已拆灯罩"})
        assert r["seq"] == 3  # 派工 seq=1，开工 seq=2，进度 seq=3

    def test_duplicate_event_is_idempotent(self, crews):
        order = order_of(crews, submit(crews))
        seq = crews.next_crew_seq(order.id)
        crews.apply_crew_event(order.id, seq, "START_WORK", {"note": "开工"},
                               actor="一班")
        again = crews.apply_crew_event(order.id, seq, "START_WORK",
                                       {"note": "开工"}, actor="一班")
        assert again["duplicate"] is True
        # 事件只记录一次，序号不重复推进
        events = crews.store.events_for("order", order.id)
        assert len([e for e in events if e.event_type == "START_WORK"]) == 1
        assert crews.store.get_order(order.id).last_seq == seq

    def test_stale_event_rejected(self, crews):
        order = order_of(crews, submit(crews))
        seq = crews.next_crew_seq(order.id)
        crews.apply_crew_event(order.id, seq, "START_WORK", {}, actor="一班")
        with pytest.raises(StaleEventError):
            crews.apply_crew_event(order.id, seq, "PROGRESS_NOTE",
                                   {"note": "过期"}, actor="一班")

    def test_seq_gap_rejected(self, crews):
        order = order_of(crews, submit(crews))
        with pytest.raises(SeqGapError):
            crews.apply_crew_event(order.id, 99, "START_WORK", {}, actor="一班")

    def test_offline_events_become_stale_after_server_event(self, crews):
        """班组离线期间调度员暂停工单，离线缓存的事件变为过期更新。"""
        order = order_of(crews, submit(crews))
        crew(crews, order.id, "START_WORK")
        crews.pause_order(order.id, reason="等配件")  # 服务端事件占用下一序号
        stale_seq = crews.store.get_order(order.id).last_seq  # 离线端以为的下一序号
        with pytest.raises(StaleEventError):
            crews.apply_crew_event(order.id, stale_seq, "PROGRESS_NOTE",
                                   {"note": "离线缓存"}, actor="一班")

    def test_invalid_transition_rejected(self, crews):
        order = order_of(crews, submit(crews))
        with pytest.raises(InvalidTransitionError):
            crew(crews, order.id, "SUBMIT_ACCEPTANCE")  # 未开工不能提交验收
        with pytest.raises(InvalidTransitionError):
            crew(crews, order.id, "TRANSFERRED")  # 非班组事件


# ----------------------------------------------------------------------
# 暂停 / 恢复 / 转交
# ----------------------------------------------------------------------

class TestPauseTransfer:
    def test_pause_and_resume_restores_status(self, crews):
        order = order_of(crews, submit(crews))
        crew(crews, order.id, "START_WORK")
        paused = crews.pause_order(order.id, reason="等配件")
        assert paused.status == OrderStatus.PAUSED
        resumed = crews.resume_order(order.id)
        assert resumed.status == OrderStatus.IN_PROGRESS

    def test_pause_not_allowed_from_pending_acceptance(self, crews):
        order = order_of(crews, submit(crews))
        crew(crews, order.id, "START_WORK")
        crew(crews, order.id, "SUBMIT_ACCEPTANCE")
        with pytest.raises(InvalidTransitionError):
            crews.pause_order(order.id, reason="不合规暂停")

    def test_transfer_requires_matching_crew(self, crews):
        order = order_of(crews, submit(crews, category="楼道扶手",
                                       address="3号楼"))
        crews.register_crew("路灯专班", ["路灯"], ["幸福里"])
        wrong = next(c for c in crews.store.all_crews() if c.name == "路灯专班")
        with pytest.raises(ServiceError):
            crews.transfer_order(order.id, wrong.id, reason="类别不匹配")
        right = next(c for c in crews.store.all_crews() if c.name == "综合班")
        moved = crews.transfer_order(order.id, right.id, reason="增援")
        assert moved.crew_id == right.id


# ----------------------------------------------------------------------
# 验收
# ----------------------------------------------------------------------

class TestAcceptance:
    def _to_acceptance(self, svc):
        order = order_of(svc, submit(svc))
        crew(svc, order.id, "START_WORK")
        crew(svc, order.id, "SUBMIT_ACCEPTANCE")
        return order.id

    def test_failed_acceptance_returns_to_rectification_only(self, crews):
        order_id = self._to_acceptance(crews)
        order = crews.submit_acceptance(
            order_id, passed=False, opinion="底座未加固", inspector="老周"
        )
        assert order.status == OrderStatus.IN_PROGRESS
        # 整改后重新提交验收
        crew(crews, order_id, "PROGRESS_NOTE", {"note": "已加固"})
        crew(crews, order_id, "SUBMIT_ACCEPTANCE")
        order = crews.submit_acceptance(
            order_id, passed=True, opinion="加固到位", inspector="老周"
        )
        assert order.status == OrderStatus.COMPLETED
        records = crews.store.acceptances_for_order(order_id)
        assert [r.round for r in records] == [1, 2]
        assert records[0].passed is False and records[1].passed is True

    def test_acceptance_requires_pending_acceptance(self, crews):
        order = order_of(crews, submit(crews))
        with pytest.raises(InvalidTransitionError):
            crews.submit_acceptance(order.id, passed=True, opinion="x",
                                    inspector="老周")

    def test_passed_acceptance_resolves_report(self, crews):
        order_id = self._to_acceptance(crews)
        crews.submit_acceptance(order_id, passed=True, opinion="合格",
                                inspector="老周")
        order = crews.store.get_order(order_id)
        report = crews.store.get_report(order.report_id)
        assert report.status == ReportStatus.RESOLVED
        assert order.completed_at is not None


# ----------------------------------------------------------------------
# 联系方式按岗位隐藏
# ----------------------------------------------------------------------

class TestContactMasking:
    def test_role_based_visibility(self, crews):
        report = submit(crews)
        full = report_view(crews, report.id, Role.DISPATCHER)
        assert full["reporter_phone"] == "13812345678"
        admin = report_view(crews, report.id, Role.ADMIN)
        assert admin["reporter_name"] == "王建国"
        masked = report_view(crews, report.id, Role.CREW)
        assert masked["reporter_phone"] == "138****5678"
        assert masked["reporter_name"] == "王**"
        hidden = report_view(crews, report.id, Role.VIEWER)
        assert hidden["reporter_phone"] == "***"
        assert hidden["reporter_name"] == "***"


# ----------------------------------------------------------------------
# 管理端总览
# ----------------------------------------------------------------------

class TestManagementOverview:
    def test_load_timeout_and_opinions(self, crews, clock):
        r1 = submit(crews, address="东门")
        r2 = submit(crews, category="楼道扶手", address="3号楼",
                    risk_level=RiskLevel.HIGH)
        o1 = order_of(crews, r1)
        o2 = order_of(crews, r2)
        crew(crews, o2.id, "START_WORK", actor="二班")
        crews.pause_order(o2.id, reason="等配件")
        crew(crews, o1.id, "START_WORK")
        crew(crews, o1.id, "SUBMIT_ACCEPTANCE")
        crews.submit_acceptance(o1.id, passed=False, opinion="需返工",
                                inspector="老周")
        clock.advance(hours=30)  # 超过 紧急4h / 高24h 时限

        overview = management_overview(crews, Role.MANAGER)
        assert len(overview["crew_load"]) == 3
        total_active = sum(c["active_orders"] for c in overview["crew_load"])
        assert total_active == 2
        reasons = {t["order_id"]: t["reason"] for t in overview["timeouts"]}
        assert o1.id in reasons and o2.id in reasons
        assert "暂停超时" in reasons[o2.id] and "等配件" in reasons[o2.id]
        assert len(overview["acceptance_opinions"]) == 1
        assert overview["acceptance_opinions"][0]["opinion"] == "需返工"
        assert overview["acceptance_opinions"][0]["passed"] is False

    def test_viewer_cannot_access_overview(self, crews):
        with pytest.raises(ServiceError):
            management_overview(crews, Role.VIEWER)


# ----------------------------------------------------------------------
# 重启持久化
# ----------------------------------------------------------------------

class TestPersistence:
    def test_state_and_events_survive_restart(self, tmp_path, clock):
        db = str(tmp_path / "restart.db")
        svc1 = RepairService(db, clock=clock)
        svc1.register_crew("维修一班", ["路灯"], ["幸福里"])
        report = submit(svc1)
        order = order_of(svc1, report)
        crew(svc1, order.id, "START_WORK")
        svc1.pause_order(order.id, reason="等配件")
        event_count = svc1.store.count_events()
        svc1.close()

        svc2 = RepairService(db, clock=clock)
        restored = svc2.store.get_order(order.id)
        assert restored.status == OrderStatus.PAUSED
        assert restored.paused_from == OrderStatus.IN_PROGRESS
        assert restored.last_seq == 3  # 派工 + 开工 + 暂停
        assert svc2.store.count_events() == event_count
        chain = svc2.get_chain(order.id)
        assert [e.event_type for e in chain["events"]] == [
            "DISPATCHED", "START_WORK", "PAUSED"
        ]
        # 重启后过期更新仍被拒绝
        with pytest.raises(StaleEventError):
            svc2.apply_crew_event(order.id, 1, "START_WORK", {}, actor="一班")
        svc2.close()

    def test_restart_continues_seq_and_idempotency(self, tmp_path, clock):
        db = str(tmp_path / "seq.db")
        svc1 = RepairService(db, clock=clock)
        svc1.register_crew("维修一班", ["路灯"], ["幸福里"])
        order = order_of(svc1, submit(svc1))
        seq = svc1.next_crew_seq(order.id)
        svc1.apply_crew_event(order.id, seq, "START_WORK", {}, actor="一班")
        svc1.close()

        svc2 = RepairService(db, clock=clock)
        # 重复发送重启前的最后一个事件 -> 幂等返回已记录结果
        dup = svc2.apply_crew_event(order.id, seq, "START_WORK", {}, actor="一班")
        assert dup["duplicate"] is True
        # 新事件在原有序号上继续递增
        nxt = svc2.apply_crew_event(order.id, seq + 1, "PROGRESS_NOTE",
                                    {"note": "重启后继续"}, actor="一班")
        assert nxt["seq"] == seq + 1
        svc2.close()
