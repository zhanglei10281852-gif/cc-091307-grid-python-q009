"""测试公共工具（标准库 unittest）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.security import Identity
from src.service import FacilityService

DISPATCHER = Identity("u-dispatch", "dispatcher", name="网格员小王")
ADMIN = Identity("u-admin", "admin", name="张主任")
CREW_EAST = Identity("u-east", "crew", crew_id="c-east", name="路灯班老李")
CREW_CENTRAL = Identity("u-central", "crew", crew_id="c-central", name="综合班老赵")
CREW_WEST = Identity("u-west", "crew", crew_id="c-west", name="扶手班老周")


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.v = start

    def __call__(self):
        return self.v

    def advance(self, hours=0.0, seconds=0.0):
        self.v += hours * 3600 + seconds


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.svc = FacilityService(":memory:", clock=self.clock)
        self.svc.register_crew("c-east", "东区路灯班", "east", ["streetlight"])
        self.svc.register_crew("c-central", "中心综合班", "central",
                               ["fitness", "handrail", "streetlight"])
        self.svc.register_crew("c-west", "西区扶手班", "west", ["handrail"])

    # -- 便捷构造 --

    def report(self, location="东区滨河路12号路灯杆", category="streetlight",
               risk=4, contact="13812345678", name="张阿姨",
               photo="灯头熄灭", source=None, identity=DISPATCHER, **kw):
        return self.svc.report(
            location=location, category=category, risk=risk,
            photo_summary=photo, reporter_name=name, reporter_contact=contact,
            identity=identity, source_id=source, **kw,
        )["ticket"]
