"""岗位权限与联系方式脱敏。"""
from src.models import State

from tests.support import ADMIN, CREW_EAST, DISPATCHER, ServiceCase


class TestSecurity(ServiceCase):
    def test_mask_phone_email_name(self):
        from src.security import mask_value
        self.assertEqual(mask_value("13812345678"), "138****5678")
        self.assertEqual(mask_value("张阿姨"), "张*姨")
        self.assertEqual(mask_value("欧阳克"), "欧*克")  # 首尾保留，中间固定一位星号
        self.assertEqual(mask_value("lijianye@example.com"), "li******@example.com")
        self.assertEqual(mask_value("ab@example.com"), "a*@example.com")
        self.assertEqual(mask_value(""), "")
        self.assertEqual(mask_value(None), "")

    def test_contact_visibility_by_role(self):
        t = self.report(source="S1")
        # 调度员：完整；管理员：脱敏；承担班组：脱敏；其他班组/匿名：隐藏
        d = self.svc.get_ticket(t.ticket_id, DISPATCHER)
        a = self.svc.get_ticket(t.ticket_id, ADMIN)
        e = self.svc.get_ticket(t.ticket_id, CREW_EAST)
        self.assertEqual(d["reporter_contact"], "13812345678")
        self.assertEqual(d["contact_visibility"], "full")
        self.assertEqual(a["reporter_contact"], "138****5678")
        self.assertEqual(e["reporter_contact"], "138****5678")
        from src.errors import PermissionDenied
        from src.security import Identity
        other_crew = Identity("u-other", "crew", crew_id="c-central")
        # 非承担班组无权查看其他班组工单
        with self.assertRaises(PermissionDenied):
            self.svc.get_ticket(t.ticket_id, other_crew)

    def test_anonymous_denied_on_list_and_merge(self):
        from src.errors import PermissionDenied
        from src.security import Identity
        t = self.report(source="S1")
        anon = Identity("x", "anonymous")
        with self.assertRaises(PermissionDenied):
            self.svc.get_ticket(t.ticket_id, anon)
        with self.assertRaises(PermissionDenied):
            self.svc.dashboard(anon)

    def test_merged_sources_masked_for_non_dispatcher(self):
        a = self.report(location="东区滨河路12号路灯杆", source="S1",
                        contact="13812345678")
        dup = self.svc.report(
            location="东区滨河路12号路灯杆", category="streetlight", risk=3,
            photo_summary="线缆外露", reporter_name="另一居民",
            reporter_contact="13700007777", identity=DISPATCHER, source_id="S2",
        )
        self.assertTrue(dup["deduped"])
        admin_view = self.svc.get_ticket(a.ticket_id, ADMIN)
        self.assertEqual(admin_view["merged_sources"][0]["reporter_contact"],
                         "137****7777")
        dispatcher_view = self.svc.get_ticket(a.ticket_id, DISPATCHER)
        self.assertEqual(dispatcher_view["merged_sources"][0]["reporter_contact"],
                         "13700007777")
