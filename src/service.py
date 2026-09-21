"""设施报修统筹领域服务。

对外提供的核心能力：
* report            登记报修（自动去重并入同设施在办件，避免重复派工）；
* supplement        补充报修信息；
* dispatch/transfer 按风险与区域派工、跨班转交；
* pause/resume      暂停（记录原因）与恢复；
* post_progress     班组移动端进度上报（事件序号幂等 + 拒绝过期更新）；
* submit/accept/reject  提交验收、验收通过、验收不通过（只回整改阶段）；
* merge             合并工单，原始来源永久可回溯；
* dashboard         班组负荷、超时原因、每项验收意见。
"""
from __future__ import annotations

import threading
import time
import unicodedata
import uuid
from typing import Any, Callable

from .aggregate import Ticket, build_ticket
from .dispatch import CrewMatcher
from .dto import event_to_dict, load_ticket, ticket_to_dict
from .errors import (
    DomainError,
    InvalidTransition,
    MergeError,
    NotFoundError,
    PermissionDenied,
)
from .events import make_event
from .models import SLA_HOURS, Category, Risk, State
from .security import Identity
from .storage import EventStore

_REGION_WORDS = (
    ("东", "east"),
    ("南", "south"),
    ("西", "west"),
    ("北", "north"),
    ("中心", "central"),
    ("中", "central"),
)


def _uid(prefix: str, n: int = 10) -> str:
    return prefix + uuid.uuid4().hex[:n]


def normalize_location(location: str) -> str:
    """设施位置归一化：全角转半角、空白压缩、小写，用于重复报修识别。"""
    text = unicodedata.normalize("NFKC", location or "")
    return "".join(text.lower().split())


def infer_region(location: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip().lower()
    for word, code in _REGION_WORDS:
        if word in (location or ""):
            return code
    return "unassigned"


class FacilityService:
    def __init__(self, db_path: str = ":memory:", clock: Callable[[], float] | None = None):
        self.store = EventStore(db_path)
        self.matcher = CrewMatcher(self.store)
        self.clock = clock or time.time
        self._lock = threading.RLock()  # 单进程内串行化命令，保证读-判-写一致

    # ---------- 班组管理 ----------

    def register_crew(
        self,
        crew_id: str,
        name: str,
        region: str,
        categories: list[str],
        active: bool = True,
    ) -> dict[str, Any]:
        cats = [Category.parse(c).value for c in categories]
        self.store.upsert_crew(crew_id, name, region, cats, active)
        return {"crew_id": crew_id, "name": name, "region": region, "categories": cats}

    # ---------- 内部工具 ----------

    def _load(self, ticket_id: str) -> Ticket:
        t = load_ticket(self.store, ticket_id)
        if t is None:
            raise NotFoundError(f"工单不存在：{ticket_id}")
        return t

    def _require_role(self, identity: Identity, *roles: str) -> None:
        if identity.role not in roles:
            raise PermissionDenied(f"需要角色 {'/'.join(roles)}，当前为 {identity.role}")

    def _append(
        self,
        ticket: Ticket,
        etype: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        *,
        identity: Identity | None = None,
        client_id: str | None = None,
        client_seq: int | None = None,
        allowed_states: tuple[State, ...] | None = None,
    ) -> tuple[Ticket, Any]:
        """读-判-写一个事件；返回 (更新后工单, 事件)。

        * (client_id, client_seq) 命中历史重发 -> 幂等返回原事件，工单照常重放；
        * allowed_states 不满足 -> InvalidTransition；
        * 序号冲突由存储层抛 StaleUpdateError（过期更新被拒绝）。
        """
        with self._lock:
            dup = self.store.find_idempotent(client_id, client_seq)
            if dup is not None:
                if dup.ticket_id != ticket.ticket_id:
                    raise InvalidTransition("幂等键已绑定其他工单")
                return self._load(ticket.ticket_id), dup

            current = self._load(ticket.ticket_id)
            if allowed_states is not None and current.state not in allowed_states:
                raise InvalidTransition(
                    f"工单 {current.ticket_id} 当前状态 {current.state.value}，"
                    f"不允许 {etype}（允许：{', '.join(s.value for s in allowed_states)}）"
                )
            seq = current.version + 1
            event = make_event(
                etype,
                current.ticket_id,
                seq,
                actor,
                client_id=client_id,
                client_seq=client_seq,
                ts=self.clock(),
                **(payload or {}),
            )
            saved = self.store.append(event, expected_seq=current.version)
            return self._load(current.ticket_id), saved

    # ---------- 报修登记 / 补充 ----------

    def report(
        self,
        location: str,
        category: object,
        risk: object = Risk.UNKNOWN,
        photo_summary: str = "",
        reporter_name: str = "",
        reporter_contact: str = "",
        region: str | None = None,
        *,
        identity: Identity | None = None,
        source_id: str | None = None,
        client_id: str | None = None,
        client_seq: int | None = None,
        auto_dispatch: bool = True,
        auto_dedupe: bool = True,
    ) -> dict[str, Any]:
        identity = identity or Identity("system", "dispatcher")
        self._require_role(identity, "dispatcher")

        cat = Category.parse(category)
        rsk = Risk.parse(risk)
        reg = infer_region(location, region)
        source_id = source_id or _uid("S")

        with self._lock:
            # 移动端重发：先按客户端幂等键短路，返回原工单
            dup = self.store.find_idempotent(client_id, client_seq)
            if dup is not None:
                return {"ticket": self._load(dup.ticket_id), "deduped": True, "event": dup}

            # 同一来源号重放：直接返回已挂接的工单
            existing = self.store.find_source(source_id)
            if existing:
                result = self._load(existing)
                return {"ticket": result, "deduped": True, "event": None}

            # 同位置 + 同类别 + 未终态（含待派工/暂停中）-> 作为补充来源并入，不再重复派工
            primary = None
            if auto_dedupe:
                primary = self._find_active_duplicate(location, cat)
            if primary is not None:
                self._attach_source(
                    primary, source_id, location, cat, rsk,
                    photo_summary, reporter_name, reporter_contact, identity,
                    client_id, client_seq,
                )
                result = self._load(primary.ticket_id)
                return {"ticket": result, "deduped": True, "event": None}

            ticket_id = _uid("T")
            event = make_event(
                "Reported", ticket_id, 1, identity.user_id,
                client_id=client_id, client_seq=client_seq, ts=self.clock(),
                location=location, region=reg, category=cat.value, risk=int(rsk),
                photo_summary=photo_summary, reporter_name=reporter_name,
                reporter_contact=reporter_contact, source_id=source_id,
            )
            saved = self.store.append(event, expected_seq=0)
            ticket = self._load(ticket_id)

            dispatch_error = None
            if auto_dispatch:
                try:
                    ticket, _ = self._dispatch_locked(ticket, identity, reason="risk_region_auto")
                except InvalidTransition as exc:
                    # 无匹配班组时保留待派工工单，交由调度员人工派工
                    dispatch_error = str(exc)
            return {"ticket": ticket, "deduped": False, "event": saved,
                    "dispatch_error": dispatch_error}

    def _find_active_duplicate(self, location: str, category: Category) -> Ticket | None:
        norm = normalize_location(location)
        for t in self._all_tickets():
            if (
                not t.is_terminal
                and t.category == category.value
                and normalize_location(t.location) == norm
            ):
                return t
        return None

    def _attach_source(
        self, primary: Ticket, source_id: str, location: str, cat: Category, rsk: Risk,
        photo_summary: str, reporter_name: str, reporter_contact: str,
        identity: Identity, client_id: str | None, client_seq: int | None,
    ) -> None:
        dup = self.store.find_idempotent(client_id, client_seq)
        if dup is not None:
            return
        current = self._load(primary.ticket_id)
        event = make_event(
            "SourceMerged", current.ticket_id, current.version + 1, identity.user_id,
            client_id=client_id, client_seq=client_seq, ts=self.clock(),
            source_id=source_id, original_ticket_id="", location=location,
            category=cat.value, risk=int(rsk), photo_summary=photo_summary,
            reporter_name=reporter_name, reporter_contact=reporter_contact,
        )
        self.store.append(event, expected_seq=current.version)

    def supplement(
        self, ticket_id: str, identity: Identity, *,
        photo_summary: str | None = None, reporter_contact: str | None = None,
        risk: object = None, note: str = "",
        client_id: str | None = None, client_seq: int | None = None,
    ) -> Ticket:
        self._require_role(identity, "dispatcher")
        ticket = self._load(ticket_id)
        if ticket.is_terminal:
            raise InvalidTransition(f"工单已终态（{ticket.state.value}），不可补充")
        ticket, _ = self._append(
            ticket, "Supplemented", identity.user_id,
            {
                "photo_summary": photo_summary or "",
                "reporter_contact": reporter_contact or "",
                "risk": None if risk is None else int(Risk.parse(risk)),
                "note": note,
            },
            identity=identity, client_id=client_id, client_seq=client_seq,
        )
        return ticket

    # ---------- 派工 / 转交 / 暂停 ----------

    def _dispatch_locked(self, ticket: Ticket, identity: Identity, reason: str,
                         crew_id: str | None = None) -> tuple[Ticket, Any]:
        current = self._load(ticket.ticket_id)
        if current.state != State.PENDING:
            raise InvalidTransition(f"仅待派工工单可派工，当前 {current.state.value}")
        crew = None
        if crew_id:
            loads = self.matcher.compute_loads()
            crew = loads.get(crew_id)
            if crew is None or not crew.active:
                raise InvalidTransition(f"班组不可用：{crew_id}")
        else:
            crew = self.matcher.select(current.region, current.category_enum, current.risk_enum)
            if crew is None:
                raise InvalidTransition(
                    f"区域 {current.region} 无在岗且可修 {current.category} 的班组"
                )
            crew_id = crew.crew_id
        payload = {
            "crew_id": crew_id,
            "region": current.region,
            "risk": int(current.risk_enum),
            "reason": reason,
        }
        seq = current.version + 1
        event = make_event("Dispatched", current.ticket_id, seq, identity.user_id,
                           ts=self.clock(), **payload)
        saved = self.store.append(event, expected_seq=current.version)
        return self._load(current.ticket_id), saved

    def dispatch(self, ticket_id: str, identity: Identity, *,
                 crew_id: str | None = None, reason: str = "manual_dispatch") -> Ticket:
        self._require_role(identity, "dispatcher")
        ticket = self._load(ticket_id)
        with self._lock:
            ticket, _ = self._dispatch_locked(ticket, identity, reason, crew_id)
        return ticket

    def transfer(self, ticket_id: str, identity: Identity, to_crew: str, reason: str = "") -> Ticket:
        self._require_role(identity, "dispatcher")
        ticket = self._load(ticket_id)
        loads = self.matcher.compute_loads()
        target = loads.get(to_crew)
        if target is None or not target.active:
            raise InvalidTransition(f"目标班组不存在或已停用：{to_crew}")
        if ticket.category not in target.categories:
            raise InvalidTransition(f"班组 {to_crew} 不具备 {ticket.category} 维修资质")
        if to_crew == ticket.crew_id:
            raise InvalidTransition("目标班组与当前班组相同")
        ticket, _ = self._append(
            ticket, "Transferred", identity.user_id,
            {"from_crew": ticket.crew_id or "", "to_crew": to_crew, "reason": reason},
            allowed_states=(State.DISPATCHED, State.IN_PROGRESS, State.RECTIFYING),
        )
        return ticket

    def pause(self, ticket_id: str, identity: Identity, reason: str) -> Ticket:
        self._require_role(identity, "dispatcher")
        ticket = self._load(ticket_id)
        if not reason.strip():
            raise InvalidTransition("暂停必须填写原因（超时归因依据）")
        ticket, _ = self._append(
            ticket, "Paused", identity.user_id, {"reason": reason},
            allowed_states=(State.DISPATCHED, State.IN_PROGRESS, State.RECTIFYING),
        )
        return ticket

    def resume(self, ticket_id: str, identity: Identity) -> Ticket:
        self._require_role(identity, "dispatcher")
        ticket = self._load(ticket_id)
        ticket, _ = self._append(
            ticket, "Resumed", identity.user_id,
            allowed_states=(State.PAUSED,),
        )
        return ticket

    # ---------- 班组移动端：进度上报 ----------

    def post_progress(self, ticket_id: str, identity: Identity, *,
                      status_text: str, phase: str | None = None, percent: int = 0,
                      client_id: str, client_seq: int,
                      base_seq: int | None = None) -> dict[str, Any]:
        """移动端进度上报。

        必须带 client_id + 客户端本地单调序号 client_seq：
        * 离线重发 / 网络重试同 (client_id, client_seq) -> 幂等返回原事件；
        * base_seq 为客户端所基于的工单事件序号，落后于当前版本 -> StaleUpdateError
          （拒绝过期更新；应先拉取最新状态再重发）；
        * 事件自身 seq 乐观锁兜底并发写冲突。
        """
        self._require_role(identity, "crew")
        ticket = self._load(ticket_id)
        if identity.crew_id != ticket.crew_id:
            raise PermissionDenied("仅承担班组可上报该工单进度")

        # 幂等重放优先：重复发送同一客户端序号，即使带了过期 base_seq 也照常返回原事件
        dup = self.store.find_idempotent(client_id, client_seq)
        if dup is not None:
            if dup.ticket_id != ticket_id:
                raise InvalidTransition("幂等键已绑定其他工单")
            return {"ticket": self._load(ticket_id), "event": dup, "idempotent": True}

        if base_seq is not None and base_seq < ticket.version:
            from .errors import StaleUpdateError

            raise StaleUpdateError(
                f"过期更新被拒绝：客户端基于事件序号 {base_seq}，"
                f"工单 {ticket_id} 当前版本 {ticket.version}，请先同步最新状态"
            )

        phase = (phase or "").strip().lower()
        allowed_phase = {
            State.DISPATCHED: {"", "dispatched", "in_progress"},
            State.IN_PROGRESS: {"", "in_progress"},
            State.RECTIFYING: {"", "rectifying"},
        }
        if ticket.state not in allowed_phase:
            raise InvalidTransition(
                f"当前状态 {ticket.state.value} 不接受进度上报"
                + ("（暂停工单请先恢复）" if ticket.state == State.PAUSED else "")
            )
        if phase not in allowed_phase[ticket.state]:
            raise InvalidTransition(
                f"不能从 {ticket.state.value} 直接上报 {phase} 阶段；"
                "验收驳回后必须停留在整改阶段"
            )
        percent = max(0, min(100, int(percent)))
        ticket, event = self._append(
            ticket, "ProgressPosted", identity.user_id,
            {"status_text": status_text, "phase": phase, "percent": percent},
            identity=identity, client_id=client_id, client_seq=client_seq,
        )
        return {"ticket": ticket, "event": event}

    # ---------- 验收链 ----------

    def submit_for_acceptance(self, ticket_id: str, identity: Identity, *,
                              summary: str = "", client_id: str | None = None,
                              client_seq: int | None = None) -> Ticket:
        self._require_role(identity, "crew", "dispatcher")
        ticket = self._load(ticket_id)
        if identity.role == "crew" and identity.crew_id != ticket.crew_id:
            raise PermissionDenied("仅承担班组可提交该工单验收")
        ticket, _ = self._append(
            ticket, "SubmittedForAcceptance", identity.user_id, {"summary": summary},
            identity=identity, client_id=client_id, client_seq=client_seq,
            allowed_states=(State.IN_PROGRESS, State.RECTIFYING),
        )
        return ticket

    def review_acceptance(self, ticket_id: str, identity: Identity, *,
                          passed: bool, opinion: str) -> Ticket:
        """验收：通过 -> ACCEPTED；不通过 -> 仅可回到 RECTIFYING（整改）。"""
        self._require_role(identity, "dispatcher", "admin")
        if not opinion.strip():
            raise InvalidTransition("验收必须填写意见（通过/驳回均需留痕）")
        ticket = self._load(ticket_id)
        etype = "Accepted" if passed else "AcceptanceRejected"
        ticket, _ = self._append(
            ticket, etype, identity.user_id,
            {"opinion": opinion, "reviewer": identity.name or identity.user_id},
            allowed_states=(State.PENDING_ACCEPTANCE,),
        )
        return ticket

    # ---------- 合并 ----------

    def merge(self, source_ticket_id: str, primary_ticket_id: str,
              identity: Identity, reason: str = "") -> Ticket:
        """把 source 工单并入 primary：两边各落一个事件，单事务原子完成。

        合并后：
        * source 状态变 MERGED 并指向 primary，不再被派工/计时；
        * primary 的 merged_sources 永久保留 source 的原始报修信息与联系方式。
        """
        self._require_role(identity, "dispatcher")
        if source_ticket_id == primary_ticket_id:
            raise MergeError("不能合并工单自身")
        with self._lock:
            source = self._load(source_ticket_id)
            primary = self._load(primary_ticket_id)
            if source.state in (State.MERGED, State.CLOSED_DUP):
                raise MergeError(f"来源工单已是合并/关闭状态：{source.state.value}")
            if source.state == State.ACCEPTED:
                raise MergeError("已验收工单不能合并，应按新报修登记或关闭重复件")
            if primary.is_terminal:
                raise MergeError(f"主工单已终态（{primary.state.value}），不能再并入")

            ts = self.clock()
            e_source = make_event(
                "Merged", source.ticket_id, source.version + 1, identity.user_id, ts=ts,
                primary_id=primary.ticket_id, location=source.location,
                category=source.category, risk=int(source.risk),
                photo_summary=source.photo_summary, reporter_name=source.reporter_name,
                reporter_contact=source.reporter_contact, source_id=source.source_id,
            )
            e_primary = make_event(
                "SourceMerged", primary.ticket_id, primary.version + 1, identity.user_id, ts=ts,
                source_id=source.source_id, original_ticket_id=source.ticket_id,
                location=source.location, category=source.category, risk=int(source.risk),
                photo_summary=source.photo_summary, reporter_name=source.reporter_name,
                reporter_contact=source.reporter_contact,
            )
            self.store.append_batch([
                (e_primary, primary.version),
                (e_source, source.version),
            ])
            # 来源号索引改指主工单：之后按原始来源号直接查到合并后的处理链
            self.store.rebind_sources(source.ticket_id, primary.ticket_id)
            return self._load(primary.ticket_id)

    def close_duplicate(self, ticket_id: str, primary_ticket_id: str,
                        identity: Identity, reason: str = "") -> Ticket:
        self._require_role(identity, "dispatcher")
        ticket = self._load(ticket_id)
        if ticket.is_terminal:
            raise InvalidTransition(f"工单已终态：{ticket.state.value}")
        ticket, _ = self._append(
            ticket, "ClosedDuplicate", identity.user_id,
            {"primary_id": primary_ticket_id, "reason": reason},
        )
        return ticket

    # ---------- 超时巡检 ----------

    def sweep_overdue(self, identity: Identity | None = None) -> list[Ticket]:
        """系统巡检：对首次超 SLA 的在办工单落 OverdueMarked 事件（原因留痕）。"""
        if identity is not None:
            self._require_role(identity, "dispatcher", "admin")
        marked: list[Ticket] = []
        with self._lock:
            for t in self._all_tickets():
                if not t.is_active or t.dispatched_at is None or t.overdue is not None:
                    continue
                sla = SLA_HOURS.get((t.category_enum, t.risk_enum))
                if sla is None:
                    continue
                paused = t.paused_total
                if t.is_paused and t.paused_at is not None:
                    paused += max(0.0, self.clock() - t.paused_at)
                elapsed = max(0.0, self.clock() - t.dispatched_at - paused) / 3600.0
                if elapsed <= sla:
                    continue
                if t.state == State.PAUSED:
                    why = f"暂停超时：{t.pause_reason}"
                elif t.state == State.RECTIFYING:
                    why = "验收驳回后整改超时"
                else:
                    why = "维修处理超时"
                t2, _ = self._append(
                    t, "OverdueMarked", "system",
                    {"reason": why, "overdue_hours": round(elapsed - sla, 2)},
                )
                marked.append(t2)
        return marked

    # ---------- 查询 ----------

    def _all_tickets(self) -> list[Ticket]:
        ids = {e.ticket_id for e in self.store.all_events()}
        return [build_ticket(self.store.load_events(tid)) for tid in ids]

    def list_tickets(self, identity: Identity) -> list[Ticket]:
        self._require_role(identity, "dispatcher", "admin", "crew")
        with self._lock:
            tickets = self._all_tickets()
        if identity.role == "crew":
            return [t for t in tickets if t.crew_id == identity.crew_id]
        return tickets

    def get_ticket(self, ticket_id: str, identity: Identity) -> dict[str, Any]:
        ticket = self._load(ticket_id)
        if identity.role == "anonymous":
            raise PermissionDenied("未识别身份")
        if identity.role == "crew" and identity.crew_id != ticket.crew_id:
            raise PermissionDenied("仅可查看本班组工单")
        return ticket_to_dict(ticket, identity, now=self.clock())

    def history(self, ticket_id: str, identity: Identity) -> list[dict[str, Any]]:
        ticket = self._load(ticket_id)
        if identity.role == "anonymous":
            raise PermissionDenied("未识别身份")
        if identity.role == "crew" and identity.crew_id != ticket.crew_id:
            raise PermissionDenied("仅可查看本班组工单")
        events = self.store.load_events(ticket_id)
        return [event_to_dict(e, identity, ticket) for e in events]

    def trace_source(self, source_id: str, identity: Identity) -> dict[str, Any]:
        """按原始报修来源号回溯其挂接的完整处理链。"""
        self._require_role(identity, "dispatcher", "admin")
        ticket_id = self.store.find_source(source_id)
        if not ticket_id:
            raise NotFoundError(f"来源号不存在：{source_id}")
        ticket = self._load(ticket_id)
        # 若来源工单已合并，顺着指针到达主工单处理链
        if ticket.state == State.MERGED and ticket.merged_into:
            ticket = self._load(ticket.merged_into)
        matched = None
        if ticket.source_id == source_id:
            matched = {"role": "primary", "source_id": ticket.source_id}
        for s in ticket.merged_sources:
            if s.source_id == source_id:
                matched = {
                    "role": "merged_source",
                    "source_id": s.source_id,
                    "original_ticket_id": s.original_ticket_id,
                }
        return {
            "source_id": source_id,
            "ticket_id": ticket_id,
            "binding": matched,
            "ticket": ticket_to_dict(ticket, identity, now=self.clock()),
        }

    def dashboard(self, identity: Identity) -> dict[str, Any]:
        """管理端：班组负荷 + 工单超时原因 + 每项验收意见。"""
        self._require_role(identity, "admin", "dispatcher")
        with self._lock:
            tickets = self._all_tickets()
            loads = self.matcher.compute_loads()
            rows = [ticket_to_dict(t, identity, now=self.clock()) for t in tickets]

        # 优先级排序：未完成在前，风险高在前，同风险早登记在前
        state_rank = {"accepted": 3, "merged": 4, "closed_dup": 5}
        rows.sort(key=lambda r: (
            state_rank.get(r["state"], 0),
            -int(r["risk"]),
            r["created_at"] or 0,
        ))

        state_counts: dict[str, int] = {}
        for r in rows:
            state_counts[r["state"]] = state_counts.get(r["state"], 0) + 1

        overdue = [
            {
                "ticket_id": r["ticket_id"],
                "crew_id": r["crew_id"],
                "category": r["category"],
                "risk": r["risk"],
                "elapsed_hours": r["elapsed_hours"],
                "sla_hours": r["sla_hours"],
                "live_reason": (r["overdue"] or {}).get("reason"),
                "recorded_reason": (r["recorded_overdue"] or {}).get("reason"),
                "pause_reason": r["pause_reason"],
                "state": r["state"],
            }
            for r in rows
            if r["overdue"] or r["recorded_overdue"]
        ]
        reviews = [
            {
                "ticket_id": r["ticket_id"],
                "seq": rv["seq"],
                "result": rv["result"],
                "opinion": rv["opinion"],
                "reviewer": rv["reviewer"],
                "ts": rv["ts"],
            }
            for r in rows for rv in r["reviews"]
        ]
        return {
            "generated_at": self.clock(),
            "crew_loads": [loads[cid].as_dict() for cid in sorted(loads)],
            "ticket_counts": state_counts,
            "tickets": rows,
            "overdue": overdue,
            "reviews": reviews,
        }


# 兼容占位包里的入口名
Service = FacilityService
