"""HTTP 端到端集成测试（真实起服务，urllib 请求）。"""
import json
import threading
import unittest
import urllib.error
import urllib.request

from src.http_app import make_server

from tests.support import FakeClock


def _headers(role=None, crew=None, user="u1", name=None):
    h = {"Content-Type": "application/json", "X-User-Id": user}
    if role:
        h["X-User-Role"] = role
    if crew:
        h["X-Crew-Id"] = crew
    if name:
        h["X-User-Name"] = name
    return h


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.httpd = make_server("127.0.0.1", 0, ":memory:", clock=self.clock)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.service.store.close()
        self.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def req(self, method, path, body=None, headers=None, expect_status=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.url(path), data=data, method=method,
                                   headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                status = resp.status
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            status = e.code
            payload = json.loads(e.read().decode())
        if expect_status is not None:
            self.assertEqual(status, expect_status, f"{method} {path}: {payload}")
        return status, payload

    def test_full_chain_over_http(self):
        # 健康检查
        s, b = self.req("GET", "/health")
        self.assertEqual(b["status"], "ok")

        # 注册班组
        for crew in [
            ("c-east", "东区路灯班", "east", ["streetlight"]),
            ("c-central", "中心综合班", "central", ["fitness", "handrail"]),
            ("c-west", "西区扶手班", "west", ["handrail"]),
        ]:
            self.req("POST", "/api/crews",
                     {"crew_id": crew[0], "name": crew[1], "region": crew[2],
                      "categories": crew[3]},
                     headers=_headers(role="dispatcher"), expect_status=201)

        # 登记紧急路灯报修
        s, b = self.req("POST", "/api/reports", {
            "location": "东区滨河路12号路灯杆", "category": "路灯", "risk": "紧急",
            "photo_summary": "灯头熄灭", "reporter_name": "张阿姨",
            "reporter_contact": "13812345678", "source_id": "S-WEB-1",
        }, headers=_headers(role="dispatcher", name="wang"), expect_status=201)
        tid = b["ticket"]["ticket_id"]
        self.assertEqual(b["ticket"]["crew_id"], "c-east")
        self.assertEqual(b["ticket"]["state"], "dispatched")
        self.assertEqual(b["ticket"]["version"], 2)

        # 重复报修：同设施 -> 200 去重并入，无新派工
        s, b2 = self.req("POST", "/api/reports", {
            "location": "东区滨河路12号路灯杆", "category": "streetlight", "risk": 3,
            "photo_summary": "线缆外露", "reporter_name": "陈师傅",
            "reporter_contact": "13700007777", "source_id": "S-WEB-2",
        }, headers=_headers(role="dispatcher"), expect_status=200)
        self.assertTrue(b2["deduped"])
        self.assertEqual(len(b2["ticket"]["merged_sources"]), 1)

        # 班组端进度：开始维修
        self.req("POST", f"/api/tickets/{tid}/progress", {
            "status_text": "登杆更换灯头", "phase": "in_progress",
            "client_id": "phone-east-01", "client_seq": 1,
        }, headers=_headers(role="crew", crew="c-east"), expect_status=200)

        # 移动端重复发送（同 client_seq）-> 幂等，version 不变
        s, dup = self.req("POST", f"/api/tickets/{tid}/progress", {
            "status_text": "完全不同的重复文案", "phase": "in_progress",
            "client_id": "phone-east-01", "client_seq": 1,
        }, headers=_headers(role="crew", crew="c-east"), expect_status=200)
        self.assertTrue(dup["idempotent"])
        self.assertEqual(dup["ticket"]["version"], 4)  # 去重 SourceMerged 占 seq=3

        # 过期更新（base_seq 落后）-> 409
        s, stale = self.req("POST", f"/api/tickets/{tid}/progress", {
            "status_text": "过期消息", "client_id": "phone-east-01",
            "client_seq": 5, "base_seq": 1,
        }, headers=_headers(role="crew", crew="c-east"), expect_status=409)
        self.assertEqual(stale["error"], "stale_update")

        # 其他班组不能上报 -> 403
        s, _ = self.req("POST", f"/api/tickets/{tid}/progress", {
            "status_text": "越权", "client_id": "x", "client_seq": 1,
        }, headers=_headers(role="crew", crew="c-west"), expect_status=403)

        # 联系方式：班组只见脱敏，调度员见全文
        _, crew_view = self.req("GET", f"/api/tickets/{tid}",
                                headers=_headers(role="crew", crew="c-east"))
        self.assertEqual(crew_view["reporter_contact"], "138****5678")
        _, disp_view = self.req("GET", f"/api/tickets/{tid}",
                                headers=_headers(role="dispatcher"))
        self.assertEqual(disp_view["reporter_contact"], "13812345678")

        # 提交验收 -> 驳回（只回整改）-> 整改 -> 复验通过
        self.req("POST", f"/api/tickets/{tid}/submit",
                 {"summary": "灯头已换", "client_id": "phone-east-01", "client_seq": 2},
                 headers=_headers(role="crew", crew="c-east"), expect_status=200)
        s, rej = self.req("POST", f"/api/tickets/{tid}/review",
                          {"passed": False, "opinion": "夜间仍频闪"},
                          headers=_headers(role="dispatcher"), expect_status=200)
        self.assertEqual(rej["state"], "rectifying")
        self.req("POST", f"/api/tickets/{tid}/progress", {
            "status_text": "更换镇流器", "phase": "rectifying",
            "client_id": "phone-east-01", "client_seq": 3,
        }, headers=_headers(role="crew", crew="c-east"), expect_status=200)
        self.req("POST", f"/api/tickets/{tid}/submit",
                 {"summary": "镇流器已换"},
                 headers=_headers(role="crew", crew="c-east"), expect_status=200)
        s, ok = self.req("POST", f"/api/tickets/{tid}/review",
                         {"passed": True, "opinion": "复验合格"},
                         headers=_headers(role="dispatcher"), expect_status=200)
        self.assertEqual(ok["state"], "accepted")

        # 历史事件链完整可查
        _, hist = self.req("GET", f"/api/tickets/{tid}/history",
                           headers=_headers(role="dispatcher"))
        types = [e["type"] for e in hist["events"]]
        self.assertEqual(types, [
            "Reported", "Dispatched", "SourceMerged", "ProgressPosted",
            "SubmittedForAcceptance", "AcceptanceRejected", "ProgressPosted",
            "SubmittedForAcceptance", "Accepted",
        ])

        # 来源回溯
        _, trace = self.req("GET", "/api/sources/S-WEB-2",
                            headers=_headers(role="dispatcher"), expect_status=200)
        self.assertEqual(trace["ticket_id"], tid)
        self.assertEqual(trace["binding"]["role"], "merged_source")

        # 管理看板
        s, board = self.req("GET", "/api/dashboard",
                            headers=_headers(role="admin"), expect_status=200)
        self.assertTrue(any(c["crew_id"] == "c-east" for c in board["crew_loads"]))
        opinions = [r["opinion"] for r in board["reviews"]]
        self.assertIn("夜间仍频闪", opinions)
        self.assertIn("复验合格", opinions)

    def test_pause_and_transfer_flow(self):
        self.req("POST", "/api/crews",
                 {"crew_id": "c-east", "name": "东", "region": "east",
                  "categories": ["streetlight"]},
                 headers=_headers(role="dispatcher"))
        self.req("POST", "/api/crews",
                 {"crew_id": "c-central", "name": "中", "region": "central",
                  "categories": ["streetlight"]},
                 headers=_headers(role="dispatcher"))
        _, b = self.req("POST", "/api/reports", {
            "location": "东区灯杆9号", "category": "streetlight", "risk": 4,
            "reporter_contact": "13812345678",
        }, headers=_headers(role="dispatcher"))
        tid = b["ticket"]["ticket_id"]

        # 暂停必须有原因
        self.req("POST", f"/api/tickets/{tid}/pause", {"reason": ""},
                 headers=_headers(role="dispatcher"), expect_status=422)
        self.req("POST", f"/api/tickets/{tid}/pause", {"reason": "暴雨暂停户外作业"},
                 headers=_headers(role="dispatcher"), expect_status=200)
        self.req("POST", f"/api/tickets/{tid}/resume", {},
                 headers=_headers(role="dispatcher"), expect_status=200)
        # 转交给具备资质的中心班
        s, t = self.req("POST", f"/api/tickets/{tid}/transfer",
                        {"to_crew": "c-central", "reason": "辖区边界调整"},
                        headers=_headers(role="dispatcher"), expect_status=200)
        self.assertEqual(t["crew_id"], "c-central")
        self.assertEqual(len(t["crew_history"]), 2)
