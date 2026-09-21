"""班组匹配：按区域 + 设施类别 + 风险等级 + 当前负荷选择班组。"""
from __future__ import annotations

from dataclasses import dataclass

from .models import RISK_WEIGHT, Category, Risk
from .storage import EventStore


@dataclass
class CrewLoad:
    crew_id: str
    name: str
    region: str
    categories: list[str]
    active: bool
    active_tickets: int
    load_score: float

    def as_dict(self) -> dict:
        return {
            "crew_id": self.crew_id,
            "name": self.name,
            "region": self.region,
            "categories": self.categories,
            "active": self.active,
            "active_tickets": self.active_tickets,
            "load_score": round(self.load_score, 2),
        }


class CrewMatcher:
    def __init__(self, store: EventStore):
        self.store = store

    def compute_loads(self) -> dict[str, CrewLoad]:
        """重放全部事件，统计各班组当前在手工单数量与加权负荷。"""
        from .aggregate import build_ticket

        events = self.store.all_events()
        by_ticket: dict[str, list] = {}
        for e in events:
            by_ticket.setdefault(e.ticket_id, []).append(e)

        counts: dict[str, int] = {}
        scores: dict[str, float] = {}
        for ev_list in by_ticket.values():
            t = build_ticket(ev_list)
            if t.is_active and t.crew_id:
                counts[t.crew_id] = counts.get(t.crew_id, 0) + 1
                scores[t.crew_id] = scores.get(t.crew_id, 0.0) + RISK_WEIGHT.get(
                    t.risk_enum, 1.0
                )

        loads: dict[str, CrewLoad] = {}
        for crew in self.store.list_crews():
            cid = crew["crew_id"]
            loads[cid] = CrewLoad(
                crew_id=cid,
                name=crew["name"],
                region=crew["region"],
                categories=crew["categories"],
                active=crew["active"],
                active_tickets=counts.get(cid, 0),
                load_score=scores.get(cid, 0.0),
            )
        return loads

    def candidates(self, region: str, category: Category) -> list[CrewLoad]:
        """同区域且可修该类别的在岗班组；区域无匹配时回退到同专长跨区班组。"""
        loads = self.compute_loads()
        cat = category.value
        same_region = [
            l for l in loads.values()
            if l.active and l.region == region and cat in l.categories
        ]
        if same_region:
            return same_region
        cross_region = [
            l for l in loads.values()
            if l.active and l.region != region and cat in l.categories
        ]
        return cross_region

    def select(self, region: str, category: Category, risk: Risk) -> CrewLoad | None:
        """风险越高越优先派：紧急件在同专长班组中选当前负荷最低者。"""
        candidates = self.candidates(region, category)
        if not candidates:
            return None
        # 全部候选择优：负荷分最低；并列时在手工单数最少；再并列按 id 稳定排序。
        return min(
            candidates,
            key=lambda l: (l.load_score, l.active_tickets, l.crew_id),
        )
