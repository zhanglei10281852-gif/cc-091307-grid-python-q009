"""报修登记、风险/区域派工、重复报修去重。"""
from src.errors import InvalidTransition
from src.models import State

from tests.support import DISPATCHER, ServiceCase


class TestReportDispatch(ServiceCase):
    def test_weekend_three_categories_dispatched_by_region_and_specialty(self):
        light = self.report(location="东区滨河路12号路灯杆", category="streetlight", risk=4)
        fit = self.report(location="中心花园健身区3号扭腰器", category="fitness", risk=3,
                          contact="lijianye@example.com", name="李大爷", photo="转盘松动")
        rail = self.report(location="西区3号楼2单元楼道扶手", category="handrail", risk=2,
                           contact="13900001111", name="王女士", photo="扶手脱落")

        self.assertEqual(light.state, State.DISPATCHED)
        self.assertEqual(light.crew_id, "c-east")
        self.assertEqual(fit.crew_id, "c-central")
        self.assertEqual(rail.crew_id, "c-west")

    def test_new_ticket_goes_to_idle_same_region_crew(self):
        self.svc.register_crew("c-east-2", "东区路灯二班", "east", ["streetlight"])
        self.report(source="S1")  # -> c-east（同为空，按 crew_id 稳定取前者）
        self.report(location="东区滨河路12号路灯杆", source="S2")  # 同设施去重，不增派工
        t2 = self.report(location="东区河西路8号灯杆", source="S3")
        # c-east 已有一单在身，c-east-2 空闲 -> 给空闲班组，体现负荷均衡
        self.assertEqual(t2.crew_id, "c-east-2")
        board = self.svc.matcher.compute_loads()
        self.assertEqual(board["c-east"].active_tickets, 1)
        self.assertEqual(board["c-east-2"].active_tickets, 1)

    def test_duplicate_report_merged_as_source_no_new_dispatch(self):
        first = self.report(source="S-DUP-1")
        again = self.svc.report(
            location="东区滨河路12号路灯杆", category="streetlight", risk=3,
            photo_summary="补充：白天发现线缆外露", reporter_name="另一位居民",
            reporter_contact="13700007777", identity=DISPATCHER, source_id="S-DUP-2",
        )
        self.assertTrue(again["deduped"])
        self.assertEqual(again["ticket"].ticket_id, first.ticket_id)
        self.assertEqual(len(first.merged_sources), 0)
        primary = again["ticket"]
        self.assertEqual(len(primary.merged_sources), 1)
        self.assertEqual(primary.merged_sources[0].source_id, "S-DUP-2")
        self.assertEqual(primary.merged_sources[0].reporter_contact, "13700007777")
        # 仍只有一次派工
        self.assertEqual(len([e for e in self.svc.history(first.ticket_id, DISPATCHER)
                              if e["type"] == "Dispatched"]), 1)

    def test_same_source_id_replay_returns_same_ticket(self):
        first = self.report(source="S-SAME")
        replay = self.svc.report(
            location="东区滨河路12号路灯杆", category="streetlight", risk=4,
            photo_summary="灯头熄灭", reporter_name="张阿姨", reporter_contact="13812345678",
            identity=DISPATCHER, source_id="S-SAME",
        )
        self.assertTrue(replay["deduped"])
        self.assertEqual(replay["ticket"].ticket_id, first.ticket_id)

    def test_no_crew_available_keeps_pending(self):
        t = self.report(location="南区公园长椅", category="other", risk=1, source="S-X")
        self.assertEqual(t.state, State.PENDING)
        self.assertIsNone(t.crew_id)

    def test_risk_chinese_alias_and_chains(self):
        t = self.report(risk="紧急")
        self.assertEqual(t.risk, 4)

    def test_duplicate_report_with_higher_risk_upgrades_primary(self):
        from src.models import Risk
        t = self.report(risk=1, source="S-LOW")  # 初报低风险
        self.assertEqual(t.risk, Risk.LOW)
        dup = self.svc.report(
            location="东区滨河路12号路灯杆", category="streetlight", risk=4,
            photo_summary="灯杆带电，有触电风险", reporter_name="第二位居民",
            reporter_contact="13700007777", identity=DISPATCHER, source_id="S-HIGH",
        )
        self.assertTrue(dup["deduped"])
        self.assertEqual(dup["ticket"].risk, Risk.CRITICAL)

    def test_event_seq_is_gapless(self):
        t = self.report(source="S-SEQ")
        seqs = [e["seq"] for e in self.svc.history(t.ticket_id, DISPATCHER)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
