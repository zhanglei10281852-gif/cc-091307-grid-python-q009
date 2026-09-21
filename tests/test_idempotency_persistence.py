"""移动端离线重发幂等、过期更新拒绝、进程重启恢复。"""
import tempfile
import unittest
from pathlib import Path

from src.errors import StaleUpdateError
from src.models import State
from src.security import Identity
from src.service import FacilityService

from tests.support import CREW_EAST, DISPATCHER, FakeClock, ServiceCase


class TestIdempotency(ServiceCase):
    def _start(self):
        t = self.report(source="S1")
        r = self.svc.post_progress(
            t.ticket_id, CREW_EAST, status_text="第一次进度", phase="in_progress",
            client_id="dev-01", client_seq=10,
        )
        return t, r

    def test_duplicate_client_seq_is_idempotent(self):
        t, r1 = self._start()
        # 网络超时导致移动端原样重发同一 client_seq（内容哪怕略有差异也必须忽略）
        r2 = self.svc.post_progress(
            t.ticket_id, CREW_EAST, status_text="重发的不同文案", phase="in_progress",
            client_id="dev-01", client_seq=10,
        )
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(r1["event"].event_id, r2["event"].event_id)
        self.assertEqual(r1["event"].seq, r2["event"].seq)
        history = self.svc.history(t.ticket_id, DISPATCHER)
        self.assertEqual(len([h for h in history if h["type"] == "ProgressPosted"]), 1)
        self.assertEqual(self.svc.get_ticket(t.ticket_id, CREW_EAST)["last_progress_text"],
                         "第一次进度")

    def test_new_seq_after_offline_burst_applies_in_order(self):
        t, _ = self._start()
        for seq, text in [(11, "离线进度A"), (12, "离线进度B"), (13, "离线进度C")]:
            self.svc.post_progress(t.ticket_id, CREW_EAST, status_text=text,
                                   client_id="dev-01", client_seq=seq)
        history = self.svc.history(t.ticket_id, DISPATCHER)
        self.assertEqual(
            [h["payload"]["status_text"] for h in history if h["type"] == "ProgressPosted"],
            ["第一次进度", "离线进度A", "离线进度B", "离线进度C"],
        )

    def test_stale_base_seq_rejected(self):
        t, r1 = self._start()
        version_now = r1["ticket"].version
        # 客户端持有旧版本 1（派工后），却基于 base_seq=1 发新事件 -> 拒绝过期更新
        with self.assertRaises(StaleUpdateError):
            self.svc.post_progress(
                t.ticket_id, CREW_EAST, status_text="过期消息",
                client_id="dev-01", client_seq=99, base_seq=1,
            )
        # 幂等重放优先于过期判断：同一条原消息即便带过期 base_seq 也应返回原事件
        r = self.svc.post_progress(
            t.ticket_id, CREW_EAST, status_text="x",
            client_id="dev-01", client_seq=10, base_seq=1,
        )
        self.assertTrue(r["idempotent"])
        self.assertEqual(version_now, r["ticket"].version)

    def test_concurrent_write_optimistic_lock(self):
        # 直接构造两个并发命令：第二个的 expected_seq 落后会被存储层拒绝
        t = self.report(source="S1")
        from src.events import make_event
        current = self.svc._load(t.ticket_id)
        e1 = make_event("ProgressPosted", t.ticket_id, current.version + 1,
                        CREW_EAST.user_id, status_text="A", phase="in_progress",
                        client_id="dev-x", client_seq=1)
        e2 = make_event("ProgressPosted", t.ticket_id, current.version + 1,
                        CREW_EAST.user_id, status_text="B", phase="in_progress",
                        client_id="dev-y", client_seq=1)
        self.svc.store.append(e1, expected_seq=current.version)
        with self.assertRaises(StaleUpdateError):
            self.svc.store.append(e2, expected_seq=current.version)


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "facility.db")
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_and_full_event_history_survive_restart(self):
        svc = FacilityService(self.db, clock=self.clock)
        svc.register_crew("c-east", "东区路灯班", "east", ["streetlight"])
        dispatcher = DISPATCHER
        t = svc.report(location="东区灯杆1号", category="streetlight", risk=4,
                       photo_summary="熄灯", reporter_name="张阿姨",
                       reporter_contact="13812345678", identity=dispatcher,
                       source_id="S-PERSIST-1")["ticket"]
        svc.post_progress(t.ticket_id, CREW_EAST, status_text="登杆", phase="in_progress",
                          client_id="dev", client_seq=1)
        svc.pause(t.ticket_id, dispatcher, reason="等配件")
        self.clock.advance(hours=2)
        svc.resume(t.ticket_id, dispatcher)
        self.clock.advance(hours=1)
        svc.submit_for_acceptance(t.ticket_id, CREW_EAST, summary="修好了")
        svc.review_acceptance(t.ticket_id, dispatcher, passed=False, opinion="仍频闪")
        tid = t.ticket_id
        svc.store.close()

        # 进程重启：新实例从事件流完整恢复
        svc2 = FacilityService(self.db, clock=self.clock)
        t2 = svc2._load(tid)
        self.assertEqual(t2.state, State.RECTIFYING)
        self.assertEqual(t2.crew_id, "c-east")
        self.assertAlmostEqual(t2.paused_total, 7200.0, places=2)
        self.assertEqual(len(t2.reviews), 1)
        self.assertEqual(t2.reviews[0].result, "rejected")
        history = svc2.history(tid, dispatcher)
        self.assertEqual(
            [h["type"] for h in history],
            ["Reported", "Dispatched", "ProgressPosted", "Paused", "Resumed",
             "SubmittedForAcceptance", "AcceptanceRejected"],
        )
        # 班组注册表也保留
        self.assertTrue(any(c["crew_id"] == "c-east" for c in svc2.store.list_crews()))
        svc2.store.close()
