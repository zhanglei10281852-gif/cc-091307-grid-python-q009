"""演示种子数据：注册班组 + 周末三类报修。

用法：python -m src.seed --db data/facility.db
"""
from __future__ import annotations

import argparse

from .security import Identity
from .service import FacilityService

DISPATCHER = Identity("u001", "dispatcher", name="网格员小王")


def seed(service: FacilityService) -> None:
    service.register_crew("crew-east-light", "东区路灯班", "east", ["streetlight"])
    service.register_crew("crew-central-fit", "中心综合维修班", "central",
                          ["fitness", "handrail", "streetlight"])
    service.register_crew("crew-west-rail", "西区扶手班", "west", ["handrail"])

    service.report(
        location="东区滨河路12号路灯杆",
        category="streetlight", risk="紧急",
        photo_summary="夜间灯头完全熄灭，路口无照明",
        reporter_name="张阿姨", reporter_contact="13812345678",
        identity=DISPATCHER, source_id="S-DEMO-LIGHT",
    )
    service.report(
        location="中心花园健身区3号扭腰器",
        category="fitness", risk="高",
        photo_summary="转盘松动，存在夹伤风险",
        reporter_name="李大爷", reporter_contact="lijianye@example.com",
        identity=DISPATCHER, source_id="S-DEMO-FIT",
    )
    service.report(
        location="西区3号楼2单元楼道扶手",
        category="handrail", risk="中",
        photo_summary="二楼转角扶手脱落一段",
        reporter_name="王女士", reporter_contact="13900001111",
        identity=DISPATCHER, source_id="S-DEMO-RAIL",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/facility.db")
    args = parser.parse_args()
    service = FacilityService(db_path=args.db)
    seed(service)
    print("种子数据已写入", args.db)
    service.store.close()


if __name__ == "__main__":
    main()
