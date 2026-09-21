"""合并工单与原始来源回溯。"""
from src.errors import MergeError
from src.models import State

from tests.support import CREW_EAST, DISPATCHER, ServiceCase


class TestMerge(ServiceCase):
    def test_manual_merge_keeps_original_sources_traceable(self):
        a = self.report(location="东区滨河路12号路灯杆", source="S-A")
        b = self.svc.report(
            location="东区滨河路 12 号 路灯杆",  # 全角/空格差异，归一化后本应自动去重
            category="streetlight", risk=3, photo_summary="灯杆锈蚀",
            reporter_name="陈师傅", reporter_contact="13611112222",
            identity=DISPATCHER, source_id="S-B",
        )
        # 归一化后 S-B 应已自动并入 S-A 主件，直接验证自动合并
        self.assertTrue(b["deduped"])
        primary = b["ticket"]
        self.assertEqual(primary.ticket_id, a.ticket_id)
        source = primary.merged_sources[0]
        self.assertEqual(source.source_id, "S-B")
        self.assertEqual(source.original_ticket_id, "")
        self.assertEqual(source.reporter_contact, "13611112222")

        # 再登记一个写法差异更大、未被自动识别的重复件，走人工合并
        c = self.svc.report(
            location="东区滨河路十二号灯杆", category="streetlight", risk=2,
            photo_summary="同一灯不亮", reporter_name="路人丙",
            reporter_contact="13522223333", identity=DISPATCHER,
            source_id="S-C", auto_dedupe=False,
        )["ticket"]
        self.assertEqual(c.state, State.DISPATCHED)
        primary = self.svc.merge(c.ticket_id, primary.ticket_id, DISPATCHER, "现场核实同杆")
        self.assertEqual(len(primary.merged_sources), 2)
        c_reloaded = self.svc._load(c.ticket_id)
        self.assertEqual(c_reloaded.state, State.MERGED)
        self.assertEqual(c_reloaded.merged_into, primary.ticket_id)

        # 按原始来源号回溯：S-C 直达主工单处理链
        trace = self.svc.trace_source("S-C", DISPATCHER)
        self.assertEqual(trace["ticket_id"], primary.ticket_id)
        self.assertEqual(trace["binding"]["role"], "merged_source")
        self.assertEqual(trace["binding"]["original_ticket_id"], c.ticket_id)
        trace_a = self.svc.trace_source("S-A", DISPATCHER)
        self.assertEqual(trace_a["binding"]["role"], "primary")

    def test_merge_atomic_and_validations(self):
        a = self.report(location="东区灯杆1号", source="S-A")
        b = self.report(location="东区灯杆2号", source="S-B")
        with self.assertRaises(MergeError):
            self.svc.merge(a.ticket_id, a.ticket_id, DISPATCHER)
        # 验收后的工单不能被并入
        self.svc.post_progress(a.ticket_id, CREW_EAST,
                               status_text="done", phase="in_progress",
                               client_id="d", client_seq=1)
        self.svc.submit_for_acceptance(a.ticket_id, CREW_EAST)
        self.svc.review_acceptance(a.ticket_id, DISPATCHER, passed=True, opinion="ok")
        with self.assertRaises(MergeError):
            self.svc.merge(a.ticket_id, b.ticket_id, DISPATCHER)
