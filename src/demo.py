"""周末报修统筹场景演示：python -m src.demo [db_path]。

覆盖：受理与自动派工、重复报修合并、离线重复/过期事件、暂停/转交、
验收不通过回到整改、管理端总览、进程重启后状态与事件保留。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from .models import RiskLevel, Role
from .service import RepairService, SeqGapError, StaleEventError
from .views import chain_view, management_overview, report_view


class ManualClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def show(title: str) -> None:
    print(f"\n{'=' * 18} {title} {'=' * 18}")


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        tempfile.mkdtemp(prefix="repair-demo-"), "repair.db"
    )
    # 周六 08:00 开始
    clock = ManualClock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    svc = RepairService(db_path, clock=clock)

    show("0. 注册维修班组")
    svc.register_crew("维修一班", ["路灯", "健身器材"], ["幸福里", "滨河"])
    svc.register_crew("维修二班", ["楼道扶手", "路灯"], ["幸福里"])
    svc.register_crew("综合保障班", ["路灯", "健身器材", "楼道扶手"],
                      ["幸福里", "滨河", "西山"])
    print("已注册：维修一班 / 维修二班 / 综合保障班")

    show("1. 周末受理：路灯(紧急) / 健身器材(中) / 楼道扶手(高)")
    r1 = svc.submit_report(
        category="路灯", region="幸福里", address="东门", risk_level=RiskLevel.URGENT,
        photo_summary="照片2张：灯杆倾斜、灯具熄灭",
        reporter_name="王建国", reporter_phone="13812345678",
    )
    r2 = svc.submit_report(
        category="健身器材", region="滨河", address="中心广场", risk_level=RiskLevel.MEDIUM,
        photo_summary="照片1张：漫步机螺丝松动",
        reporter_name="李秀兰", reporter_phone="13998765432",
    )
    r3 = svc.submit_report(
        category="楼道扶手", region="幸福里", address="3号楼2单元", risk_level=RiskLevel.HIGH,
        photo_summary="照片3张：扶手焊接处脱落",
        reporter_name="张志强", reporter_phone="13711112222",
    )
    for r in (r1, r2, r3):
        chain = svc.get_chain(svc.store.active_order_for_report(r.id).id)
        o = chain["order"]
        print(f"{r.category}({r.risk_level.label}) -> {o.id} [{o.status.value}] "
              f"班组={o.crew_id and svc.store.get_crew(o.crew_id).name}")
        print(f"   优先级依据: {o.priority_reason}")

    show("2. 同一设施重复报修 -> 自动合并，来源可回溯")
    dup = svc.submit_report(
        category="路灯", region="幸福里", address="东门", risk_level=RiskLevel.LOW,
        photo_summary="照片1张：同一盏路灯不亮",
        reporter_name="刘芳", reporter_phone="13655556666",
    )
    print(f"重复报修 {dup.id} 状态={dup.status.value} merged_into={dup.merged_into}")
    trace = svc.trace_report(dup.id)
    print(f"主单={trace['master'].id}，来源单={[s.id for s in trace['sources']]}")
    print(f"主单照片摘要已保留原始记录，来源单事件数={len(trace['report_events'])}")

    show("3. 班组移动端：离线重发(幂等) / 过期更新(拒绝) / 跳号(拒绝)")
    wo1 = svc.store.active_order_for_report(r1.id).id
    s = svc.next_crew_seq(wo1)
    res = svc.apply_crew_event(wo1, s, "START_WORK", {"note": "到场开工"}, actor="维修一班")
    print(f"开工 seq={s} -> 状态={res['status']}")
    retry = svc.apply_crew_event(wo1, s, "START_WORK", {"note": "到场开工"}, actor="维修一班")
    print(f"离线重发同一事件 seq={s} -> duplicate={retry['duplicate']}（幂等，不重复处理）")
    try:
        svc.apply_crew_event(wo1, s, "PROGRESS_NOTE", {"note": "篡改的过期事件"}, actor="维修一班")
    except StaleEventError as e:
        print(f"过期更新 seq={s} 内容不一致 -> 拒绝: {e}")
    try:
        svc.apply_crew_event(wo1, 99, "PROGRESS_NOTE", {"note": "跳号"}, actor="维修一班")
    except SeqGapError as e:
        print(f"跳号 seq=99 -> 拒绝: {e}")
    s = svc.next_crew_seq(wo1)
    svc.apply_crew_event(wo1, s, "PROGRESS_NOTE", {"note": "已更换灯具，待通电测试"}, actor="维修一班")

    show("4. 暂停 / 转交 / 补充")
    wo3 = svc.store.active_order_for_report(r3.id).id
    svc.apply_crew_event(wo3, svc.next_crew_seq(wo3), "START_WORK", {}, actor="维修二班")
    svc.pause_order(wo3, reason="等待扶手配件到货", actor="调度员小赵")
    print(f"工单 {wo3} 已暂停（等配件）")
    svc.resume_order(wo3, actor="调度员小赵")
    zonghe = next(c for c in svc.store.all_crews() if c.name == "综合保障班")
    svc.transfer_order(wo3, zonghe.id, reason="二班人手不足，转综合保障班", actor="调度员小赵")
    print(f"工单 {wo3} 已恢复并转交综合保障班")
    svc.supplement_report(r3.id, actor="网格员", note="老人通行频繁，请优先处理",
                          risk_level=RiskLevel.URGENT)
    print(f"报修 {r3.id} 已补充并升级为紧急，工单优先级={svc.store.get_order(wo3).priority}")

    show("5. 验收：不通过只能回到整改阶段")
    svc.apply_crew_event(wo1, svc.next_crew_seq(wo1), "SUBMIT_ACCEPTANCE",
                         {"summary": "路灯已修复"}, actor="维修一班")
    svc.submit_acceptance(wo1, passed=False, opinion="灯杆仍有倾斜，需加固底座",
                          inspector="验收员老周")
    o = svc.store.get_order(wo1)
    print(f"验收不通过 -> 状态={o.status.value}（只能回整改）")
    svc.apply_crew_event(wo1, svc.next_crew_seq(wo1), "PROGRESS_NOTE",
                         {"note": "底座已加固"}, actor="维修一班")
    svc.apply_crew_event(wo1, svc.next_crew_seq(wo1), "SUBMIT_ACCEPTANCE",
                         {"summary": "整改完成"}, actor="维修一班")
    svc.submit_acceptance(wo1, passed=True, opinion="加固到位，照明正常", inspector="验收员老周")
    print(f"二次验收通过 -> 状态={svc.store.get_order(wo1).status.value}")

    show("6. 联系方式按岗位权限隐藏")
    for role in (Role.DISPATCHER, Role.CREW, Role.VIEWER):
        v = report_view(svc, r1.id, role)
        print(f"{role.value}: 姓名={v['reporter_name']} 电话={v['reporter_phone']}")

    show("7. 时钟推进 -> 管理端总览（负荷 / 超时原因 / 验收意见）")
    clock.advance(hours=30)
    overview = management_overview(svc, Role.MANAGER)
    for c in overview["crew_load"]:
        print(f"班组 {c['crew_name']}: 负荷 {c['active_orders']}/{c['capacity']} "
              f"明细={c['by_status']} 超时={c['overdue_orders']}")
    for t in overview["timeouts"]:
        print(f"超时工单 {t['order_id']} [{t['facility']}] 超{t['overdue_hours']}h: {t['reason']}")
    for a in overview["acceptance_opinions"]:
        print(f"验收意见 工单{a['order_id']} 第{a['round']}轮 "
              f"{'通过' if a['passed'] else '不通过'}: {a['opinion']}")

    show("8. 模拟进程重启：状态与历史事件完整保留")
    svc.close()
    svc2 = RepairService(db_path, clock=clock)
    o = svc2.store.get_order(wo1)
    print(f"重启后工单 {wo1}: 状态={o.status.value} last_seq={o.last_seq}")
    print(f"重启后事件总数={svc2.store.count_events()}，处理链事件数="
          f"{len(svc2.get_chain(wo1)['events'])}")
    v = chain_view(svc2, wo1, Role.MANAGER)
    print(f"处理链时间线: " + " -> ".join(e["type"] for e in v["timeline"]))
    svc2.close()
    print(f"\n数据库文件: {db_path}")


if __name__ == "__main__":
    main()
