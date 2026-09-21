"""处理链流转：补充、转交、暂停/恢复、验收（驳回只能回整改）。"""
from src.errors import InvalidTransition
from src.models import State

from tests.support import (
    CREW_CENTRAL, CREW_EAST, DISPATCHER, ServiceCase,
)


class TestLifecycle(ServiceCase):
    def test_supplement_updates_fields_and_keeps_event(self):
        t = self.report(source="S1")
        t = self.svc.supplement(
            t.ticket_id, DISPATCHER, photo_summary="新照片：灯杆倾斜", risk=3,
        )
        self.assertIn("灯杆倾斜", t.photo_summary)
        self.assertEqual(t.risk, 4)  # 风险只升不降
        types = [e["type"] for e in self.svc.history(t.ticket_id, DISPATCHER)]
        self.assertIn("Supplemented", types)

    def test_transfer_requires_specialty_and_records_chain(self):
        t = self.report(source="S1")  # streetlight -> c-east
        # c-west 只会扶手，不能接路灯
        with self.assertRaises(InvalidTransition):
            self.svc.transfer(t.ticket_id, DISPATCHER, "c-west", "跨班试试")
        # c-central 可修路灯，可以接
        t = self.svc.transfer(t.ticket_id, DISPATCHER, "c-central", "东区班抢修冲突")
        self.assertEqual(t.crew_id, "c-central")
        self.assertEqual([h["via"] for h in t.crew_history], ["dispatch", "transfer"])
        self.assertEqual(t.crew_history[-1]["from_crew"], "c-east")

    def test_pause_requires_reason_and_resume_restores_phase(self):
        t = self.report(source="S1")
        self.svc.post_progress(
            t.ticket_id, CREW_EAST, status_text="登杆检查", phase="in_progress",
            client_id="dev-east", client_seq=1,
        )
        with self.assertRaises(InvalidTransition):
            self.svc.pause(t.ticket_id, DISPATCHER, reason="  ")
        t = self.svc.pause(t.ticket_id, DISPATCHER, reason="等待防触电配件到货")
        self.assertEqual(t.state, State.PAUSED)
        self.assertEqual(t.paused_from, State.IN_PROGRESS)
        # 暂停中不能上报进度
        from src.errors import DomainError
        with self.assertRaises(DomainError):
            self.svc.post_progress(
                t.ticket_id, CREW_EAST, status_text="偷跑", client_id="dev-east", client_seq=2,
            )
        t = self.svc.resume(t.ticket_id, DISPATCHER)
        self.assertEqual(t.state, State.IN_PROGRESS)
        # 恢复时结算上一段暂停时长；再暂停恢复一次后应累计
        self.svc.pause(t.ticket_id, DISPATCHER, reason="又等一个配件")
        self.clock.advance(hours=1)
        t = self.svc.resume(t.ticket_id, DISPATCHER)
        self.assertGreater(t.paused_total, 0.0)

    def test_reject_acceptance_only_goes_rectifying_then_pass(self):
        t = self.report(source="S1")
        self.svc.post_progress(t.ticket_id, CREW_EAST, status_text="更换灯头完成",
                               phase="in_progress", percent=100,
                               client_id="dev-east", client_seq=1)
        t = self.svc.submit_for_acceptance(t.ticket_id, CREW_EAST, summary="已更换 LED 灯头")
        self.assertEqual(t.state, State.PENDING_ACCEPTANCE)

        # 验收意见必填
        with self.assertRaises(InvalidTransition):
            self.svc.review_acceptance(t.ticket_id, DISPATCHER, passed=False, opinion="  ")

        # 不通过：只能回到整改阶段
        t = self.svc.review_acceptance(
            t.ticket_id, DISPATCHER, passed=False, opinion="夜间实测仍有频闪，请检修镇流器",
        )
        self.assertEqual(t.state, State.RECTIFYING)

        # 整改中不能直接提交？可以提交复验，但必须先报整改进度；直接提交也允许
        self.svc.post_progress(t.ticket_id, CREW_EAST, status_text="更换镇流器",
                               phase="rectifying", client_id="dev-east", client_seq=2)
        t = self.svc.submit_for_acceptance(t.ticket_id, CREW_EAST, summary="镇流器已更换")
        t = self.svc.review_acceptance(t.ticket_id, DISPATCHER, passed=True,
                                       opinion="复验合格，照明恢复")
        self.assertEqual(t.state, State.ACCEPTED)
        results = [r.result for r in t.reviews]
        self.assertEqual(results, ["rejected", "passed"])
        opinions = [r.opinion for r in t.reviews]
        self.assertIn("夜间实测仍有频闪，请检修镇流器", opinions)

    def test_cannot_skip_rectifying_via_progress_phase(self):
        t = self.report(source="S1")
        self.svc.post_progress(t.ticket_id, CREW_EAST, status_text="维修中",
                               phase="in_progress", client_id="d", client_seq=1)
        self.svc.submit_for_acceptance(t.ticket_id, CREW_EAST)
        self.svc.review_acceptance(t.ticket_id, DISPATCHER, passed=False, opinion="不合格")
        # 整改阶段不允许把阶段报成 in_progress 跳过整改
        with self.assertRaises(InvalidTransition):
            self.svc.post_progress(t.ticket_id, CREW_EAST, status_text="想跳过",
                                   phase="in_progress", client_id="d", client_seq=2)

    def test_only_assigned_crew_may_post(self):
        from src.errors import PermissionDenied
        t = self.report(source="S1")  # -> c-east
        with self.assertRaises(PermissionDenied):
            self.svc.post_progress(t.ticket_id, CREW_CENTRAL, status_text="越权",
                                   client_id="d", client_seq=1)
