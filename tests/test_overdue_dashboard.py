"""SLA 超时判定（暂停不计工时）、超时原因、管理端看板。"""
from tests.support import ADMIN, CREW_EAST, DISPATCHER, ServiceCase


class TestOverdueDashboard(ServiceCase):
    def test_overdue_marked_with_reason_and_pause_excluded(self):
        t = self.report(source="S1")  # streetlight + critical -> SLA 4h
        self.svc.post_progress(t.ticket_id, CREW_EAST, status_text="处理中",
                               phase="in_progress", client_id="d", client_seq=1)
        # 3 小时后暂停等配件
        self.clock.advance(hours=3)
        self.svc.pause(t.ticket_id, DISPATCHER, reason="防触电配件缺货，供应商 48h 发货")
        self.clock.advance(hours=47)
        marked = self.svc.sweep_overdue()
        self.assertEqual(marked, [])  # 暂停时长不计 SLA：实际工时仍 3h

        self.svc.resume(t.ticket_id, DISPATCHER)
        self.clock.advance(hours=2)  # 工时 5h > 4h
        marked = self.svc.sweep_overdue()
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0].ticket_id, t.ticket_id)
        self.assertEqual(marked[0].overdue["reason"], "维修处理超时")

        board = self.svc.dashboard(ADMIN)
        row = next(r for r in board["overdue"] if r["ticket_id"] == t.ticket_id)
        self.assertEqual(row["recorded_reason"], "维修处理超时")
        self.assertIn("超过 4 小时 SLA", row["live_reason"])
        # 只标记一次（事件留痕，不重复刷）
        self.clock.advance(hours=10)
        self.assertEqual(self.svc.sweep_overdue(), [])

    def test_overdue_reason_rectifying_after_rejection(self):
        t = self.report(source="S1")
        self.svc.post_progress(t.ticket_id, CREW_EAST, status_text="修完",
                               phase="in_progress", percent=100,
                               client_id="d", client_seq=1)
        self.svc.submit_for_acceptance(t.ticket_id, CREW_EAST)
        self.clock.advance(hours=3)
        self.svc.review_acceptance(t.ticket_id, DISPATCHER, passed=False,
                                   opinion="接线不规范，重新做防水接头")
        self.clock.advance(hours=3)  # 累计 6h > critical 4h
        marked = self.svc.sweep_overdue()
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0].overdue["reason"], "验收驳回后整改超时")

    def test_dashboard_shows_loads_and_every_review_opinion(self):
        t1 = self.report(location="东区灯杆1号", source="S1")
        t2 = self.report(location="东区灯杆2号", source="S2")
        self.svc.post_progress(t1.ticket_id, CREW_EAST, status_text="x",
                               phase="in_progress", client_id="d", client_seq=1)
        self.svc.post_progress(t2.ticket_id, CREW_EAST, status_text="y",
                               phase="in_progress", client_id="d", client_seq=2)
        self.svc.submit_for_acceptance(t1.ticket_id, CREW_EAST)
        self.svc.review_acceptance(t1.ticket_id, DISPATCHER, passed=False,
                                   opinion="接头裸露")
        self.svc.post_progress(t1.ticket_id, CREW_EAST, status_text="包好",
                               phase="rectifying", client_id="d", client_seq=3)
        self.svc.submit_for_acceptance(t1.ticket_id, CREW_EAST)
        self.svc.review_acceptance(t1.ticket_id, DISPATCHER, passed=True,
                                   opinion="整改到位")

        board = self.svc.dashboard(ADMIN)
        east = next(c for c in board["crew_loads"] if c["crew_id"] == "c-east")
        self.assertEqual(east["active_tickets"], 1)  # t1 已验收，仅 t2 在办
        self.assertGreater(east["load_score"], 0)
        opinions = {r["opinion"] for r in board["reviews"]}
        self.assertIn("接头裸露", opinions)
        self.assertIn("整改到位", opinions)
        # 每项验收意见都可定位到工单
        for r in board["reviews"]:
            self.assertTrue(r["ticket_id"])
            self.assertIn(r["result"], ("passed", "rejected"))
        self.assertIn("in_progress", board["ticket_counts"])
