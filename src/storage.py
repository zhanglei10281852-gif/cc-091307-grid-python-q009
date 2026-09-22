"""SQLite 持久化：状态表 + 完整事件日志，进程重启后派工状态与历史事件完整保留。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from .models import (
    AcceptanceRecord,
    Crew,
    Event,
    OrderStatus,
    Report,
    ReportStatus,
    RiskLevel,
    WorkOrder,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    facility_key TEXT NOT NULL,
    category TEXT NOT NULL,
    region TEXT NOT NULL,
    address TEXT NOT NULL,
    risk_level INTEGER NOT NULL,
    photo_summary TEXT NOT NULL,
    reporter_name TEXT NOT NULL,
    reporter_phone TEXT NOT NULL,
    status TEXT NOT NULL,
    merged_into TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_facility ON reports(facility_key, status);
CREATE INDEX IF NOT EXISTS idx_reports_merged_into ON reports(merged_into);

CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    crew_id TEXT,
    status TEXT NOT NULL,
    paused_from TEXT,
    priority INTEGER NOT NULL,
    priority_reason TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_report ON work_orders(report_id);
CREATE INDEX IF NOT EXISTS idx_orders_crew ON work_orders(crew_id, status);

CREATE TABLE IF NOT EXISTS crews (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    categories TEXT NOT NULL,
    regions TEXT NOT NULL,
    max_parallel INTEGER NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (aggregate_type, aggregate_id, seq)
);

CREATE TABLE IF NOT EXISTS acceptances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES work_orders(id),
    round INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    opinion TEXT NOT NULL,
    inspector TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _load(text: Optional[str]) -> object:
    return json.loads(text) if text else None


class SQLiteStorage:
    """线程安全的 SQLite 存储；每个公开写方法需在 tx() 中调用。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # 显式事务
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[None]:
        """显式事务：状态变更与事件追加要么全部提交，要么全部回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 报修单 ----

    def insert_report(self, report: Report) -> None:
        self._conn.execute(
            """INSERT INTO reports
               (id, facility_key, category, region, address, risk_level,
                photo_summary, reporter_name, reporter_phone, status,
                merged_into, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                report.id, report.facility_key, report.category, report.region,
                report.address, int(report.risk_level), report.photo_summary,
                report.reporter_name, report.reporter_phone, report.status.value,
                report.merged_into, report.created_at.isoformat(),
                report.updated_at.isoformat(),
            ),
        )

    def update_report(self, report: Report) -> None:
        self._conn.execute(
            """UPDATE reports SET risk_level=?, photo_summary=?, status=?,
               merged_into=?, updated_at=? WHERE id=?""",
            (
                int(report.risk_level), report.photo_summary, report.status.value,
                report.merged_into, report.updated_at.isoformat(), report.id,
            ),
        )

    def _row_to_report(self, row: sqlite3.Row) -> Report:
        return Report(
            id=row["id"], category=row["category"], region=row["region"],
            address=row["address"], risk_level=RiskLevel(row["risk_level"]),
            photo_summary=row["photo_summary"],
            reporter_name=row["reporter_name"],
            reporter_phone=row["reporter_phone"],
            status=ReportStatus(row["status"]), merged_into=row["merged_into"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_report(self, report_id: str) -> Optional[Report]:
        row = self._conn.execute(
            "SELECT * FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        return self._row_to_report(row) if row else None

    def find_open_report(self, facility_key: str) -> Optional[Report]:
        """同一设施仍处于处理中的最早报修单（用于重复报修识别）。"""
        row = self._conn.execute(
            """SELECT * FROM reports WHERE facility_key=? AND status IN (?,?)
               ORDER BY created_at LIMIT 1""",
            (facility_key, ReportStatus.PENDING.value, ReportStatus.ACTIVE.value),
        ).fetchone()
        return self._row_to_report(row) if row else None

    def reports_merged_into(self, master_id: str) -> list[Report]:
        rows = self._conn.execute(
            "SELECT * FROM reports WHERE merged_into=? ORDER BY created_at",
            (master_id,),
        ).fetchall()
        return [self._row_to_report(r) for r in rows]

    def all_reports(self) -> list[Report]:
        rows = self._conn.execute(
            "SELECT * FROM reports ORDER BY created_at"
        ).fetchall()
        return [self._row_to_report(r) for r in rows]

    # ---- 工单 ----

    def insert_order(self, order: WorkOrder) -> None:
        self._conn.execute(
            """INSERT INTO work_orders
               (id, report_id, crew_id, status, paused_from, priority,
                priority_reason, last_seq, created_at, updated_at, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order.id, order.report_id, order.crew_id, order.status.value,
                order.paused_from.value if order.paused_from else None,
                order.priority, order.priority_reason, order.last_seq,
                order.created_at.isoformat(), order.updated_at.isoformat(),
                order.completed_at.isoformat() if order.completed_at else None,
            ),
        )

    def update_order(self, order: WorkOrder) -> None:
        self._conn.execute(
            """UPDATE work_orders SET crew_id=?, status=?, paused_from=?,
               priority=?, priority_reason=?, last_seq=?, updated_at=?,
               completed_at=? WHERE id=?""",
            (
                order.crew_id, order.status.value,
                order.paused_from.value if order.paused_from else None,
                order.priority, order.priority_reason, order.last_seq,
                order.updated_at.isoformat(),
                order.completed_at.isoformat() if order.completed_at else None,
                order.id,
            ),
        )

    def _row_to_order(self, row: sqlite3.Row) -> WorkOrder:
        return WorkOrder(
            id=row["id"], report_id=row["report_id"], crew_id=row["crew_id"],
            status=OrderStatus(row["status"]),
            paused_from=OrderStatus(row["paused_from"]) if row["paused_from"] else None,
            priority=row["priority"], priority_reason=row["priority_reason"],
            last_seq=row["last_seq"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"] else None
            ),
        )

    def get_order(self, order_id: str) -> Optional[WorkOrder]:
        row = self._conn.execute(
            "SELECT * FROM work_orders WHERE id=?", (order_id,)
        ).fetchone()
        return self._row_to_order(row) if row else None

    def active_order_for_report(self, report_id: str) -> Optional[WorkOrder]:
        row = self._conn.execute(
            """SELECT * FROM work_orders WHERE report_id=?
               AND status NOT IN (?,?) ORDER BY created_at LIMIT 1""",
            (report_id, OrderStatus.COMPLETED.value, OrderStatus.CANCELLED.value),
        ).fetchone()
        return self._row_to_order(row) if row else None

    def all_orders(self) -> list[WorkOrder]:
        rows = self._conn.execute(
            "SELECT * FROM work_orders ORDER BY created_at"
        ).fetchall()
        return [self._row_to_order(r) for r in rows]

    # ---- 班组 ----

    def insert_crew(self, crew: Crew) -> None:
        self._conn.execute(
            """INSERT INTO crews (id, name, categories, regions, max_parallel)
               VALUES (?,?,?,?,?)""",
            (
                crew.id, crew.name, _dump(list(crew.categories)),
                _dump(list(crew.regions)), crew.max_parallel,
            ),
        )

    def _row_to_crew(self, row: sqlite3.Row) -> Crew:
        return Crew(
            id=row["id"], name=row["name"],
            categories=tuple(json.loads(row["categories"])),
            regions=tuple(json.loads(row["regions"])),
            max_parallel=row["max_parallel"],
        )

    def get_crew(self, crew_id: str) -> Optional[Crew]:
        row = self._conn.execute(
            "SELECT * FROM crews WHERE id=?", (crew_id,)
        ).fetchone()
        return self._row_to_crew(row) if row else None

    def all_crews(self) -> list[Crew]:
        rows = self._conn.execute("SELECT * FROM crews ORDER BY name").fetchall()
        return [self._row_to_crew(r) for r in rows]

    # ---- 事件 ----

    def append_event(self, event: Event) -> int:
        cur = self._conn.execute(
            """INSERT INTO events
               (aggregate_type, aggregate_id, seq, event_type, actor,
                payload, result, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                event.aggregate_type, event.aggregate_id, event.seq,
                event.event_type, event.actor, _dump(event.payload),
                _dump(event.result) if event.result is not None else None,
                event.created_at.isoformat(),
            ),
        )
        return cur.lastrowid

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"], aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"], seq=row["seq"],
            event_type=row["event_type"], actor=row["actor"],
            payload=json.loads(row["payload"]),
            result=_load(row["result"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_event(
        self, aggregate_type: str, aggregate_id: str, seq: int
    ) -> Optional[Event]:
        row = self._conn.execute(
            """SELECT * FROM events WHERE aggregate_type=? AND aggregate_id=?
               AND seq=?""",
            (aggregate_type, aggregate_id, seq),
        ).fetchone()
        return self._row_to_event(row) if row else None

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[Event]:
        rows = self._conn.execute(
            """SELECT * FROM events WHERE aggregate_type=? AND aggregate_id=?
               ORDER BY seq""",
            (aggregate_type, aggregate_id),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def max_seq(self, aggregate_type: str, aggregate_id: str) -> int:
        row = self._conn.execute(
            """SELECT COALESCE(MAX(seq), 0) AS m FROM events
               WHERE aggregate_type=? AND aggregate_id=?""",
            (aggregate_type, aggregate_id),
        ).fetchone()
        return row["m"]

    def count_events(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]

    # ---- 验收 ----

    def insert_acceptance(self, record: AcceptanceRecord) -> int:
        cur = self._conn.execute(
            """INSERT INTO acceptances
               (order_id, round, passed, opinion, inspector, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                record.order_id, record.round, int(record.passed),
                record.opinion, record.inspector,
                record.created_at.isoformat(),
            ),
        )
        return cur.lastrowid

    def _row_to_acceptance(self, row: sqlite3.Row) -> AcceptanceRecord:
        return AcceptanceRecord(
            id=row["id"], order_id=row["order_id"], round=row["round"],
            passed=bool(row["passed"]), opinion=row["opinion"],
            inspector=row["inspector"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def acceptances_for_order(self, order_id: str) -> list[AcceptanceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM acceptances WHERE order_id=? ORDER BY round",
            (order_id,),
        ).fetchall()
        return [self._row_to_acceptance(r) for r in rows]

    def all_acceptances(self) -> list[AcceptanceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM acceptances ORDER BY created_at"
        ).fetchall()
        return [self._row_to_acceptance(r) for r in rows]
