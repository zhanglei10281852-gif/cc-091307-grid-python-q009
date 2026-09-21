"""SQLite 事件存储与班组注册表。

只持久化两类数据：
1. events  —— 不可变事件流（工单状态由事件重放得到，重启后完整恢复）；
2. crews   —— 班组注册表（区域、可修类别、在岗状态）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .events import Event, event_from_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    ticket_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    etype      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    ts         REAL NOT NULL,
    client_id  TEXT,
    client_seq INTEGER,
    payload    TEXT NOT NULL DEFAULT '{}',
    UNIQUE(ticket_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idem
    ON events(client_id, client_seq) WHERE client_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crews (
    crew_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL,
    categories TEXT NOT NULL DEFAULT '[]',
    active     INTEGER NOT NULL DEFAULT 1
);
"""


class EventStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 事件写入 ----

    def find_idempotent(self, client_id: str | None, client_seq: int | None) -> Event | None:
        """按客户端幂等键查找原事件（离线重发命中时返回原事件）。"""
        if not client_id or client_seq is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE client_id=? AND client_seq=?",
                (client_id, client_seq),
            ).fetchone()
        return event_from_row(row) if row else None

    def append(self, event: Event, expected_seq: int) -> Event:
        """在一个事务内完成：序号乐观锁检查 + 幂等键检查 + 落库。

        expected_seq 为该工单写入前应有的最大序号；不一致即过期更新，拒绝写入。
        若 (client_id, client_seq) 已存在，直接返回原事件（幂等成功，不报错）。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if event.client_id and event.client_seq is not None:
                    row = self._conn.execute(
                        "SELECT * FROM events WHERE client_id=? AND client_seq=?",
                        (event.client_id, event.client_seq),
                    ).fetchone()
                    if row is not None:
                        # 同幂等键但指向不同工单，属于客户端串号，拒绝。
                        if row["ticket_id"] != event.ticket_id:
                            raise sqlite3.IntegrityError(
                                "idempotency key bound to another ticket"
                            )
                        self._conn.execute("COMMIT")
                        return event_from_row(row)

                current = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events WHERE ticket_id=?",
                    (event.ticket_id,),
                ).fetchone()[0]
                if current != expected_seq:
                    self._conn.execute("ROLLBACK")
                    from .errors import StaleUpdateError

                    raise StaleUpdateError(
                        f"工单 {event.ticket_id} 当前版本 {current}，"
                        f"拒绝基于版本 {expected_seq} 的写入（期望下一序号 {current + 1}）"
                    )

                row = event.to_row()
                self._conn.execute(
                    """INSERT INTO events
                       (event_id, ticket_id, seq, etype, actor, ts,
                        client_id, client_seq, payload)
                       VALUES (:event_id, :ticket_id, :seq, :etype, :actor, :ts,
                               :client_id, :client_seq, :payload)""",
                    row,
                )
                if "source_id" in event.payload() and event.payload()["source_id"]:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO sources(source_id, ticket_id) VALUES(?,?)",
                        (event.payload()["source_id"], event.ticket_id),
                    )
                self._conn.execute("COMMIT")
                return event
            except Exception:
                # 事务仍活跃时回滚（幂等命中路径已自行 COMMIT）
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    # ---- 批量写入（多工单原子操作） ----

    def append_batch(self, items: list[tuple[Event, int]]) -> list[Event]:
        """单事务批量写入：[(event, expected_seq), ...]，任一项失败整体回滚。

        用于合并等需要同时修改多个工单的操作。同样处理客户端幂等键：
        批内若某事件的幂等键已存在，返回原事件并跳过该项（不视为失败）。
        """
        from .errors import StaleUpdateError

        results: list[Event] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for event, expected_seq in items:
                    if event.client_id and event.client_seq is not None:
                        row = self._conn.execute(
                            "SELECT * FROM events WHERE client_id=? AND client_seq=?",
                            (event.client_id, event.client_seq),
                        ).fetchone()
                        if row is not None:
                            if row["ticket_id"] != event.ticket_id:
                                raise sqlite3.IntegrityError(
                                    "idempotency key bound to another ticket"
                                )
                            results.append(event_from_row(row))
                            continue

                    current = self._conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE ticket_id=?",
                        (event.ticket_id,),
                    ).fetchone()[0]
                    if current != expected_seq:
                        raise StaleUpdateError(
                            f"工单 {event.ticket_id} 当前版本 {current}，"
                            f"拒绝基于版本 {expected_seq} 的写入"
                        )

                    row = event.to_row()
                    self._conn.execute(
                        """INSERT INTO events
                           (event_id, ticket_id, seq, etype, actor, ts,
                            client_id, client_seq, payload)
                           VALUES (:event_id, :ticket_id, :seq, :etype, :actor, :ts,
                                   :client_id, :client_seq, :payload)""",
                        row,
                    )
                    payload = event.payload()
                    if "source_id" in payload and payload["source_id"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO sources(source_id, ticket_id) VALUES(?,?)",
                            (payload["source_id"], event.ticket_id),
                        )
                    results.append(event)
                self._conn.execute("COMMIT")
                return results
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    # ---- 事件读取 ----

    def load_events(self, ticket_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE ticket_id=? ORDER BY seq", (ticket_id,)
            ).fetchall()
        return [event_from_row(r) for r in rows]

    def all_events(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY ticket_id, seq"
            ).fetchall()
        return [event_from_row(r) for r in rows]

    def max_seq(self, ticket_id: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()[0]

    def find_source(self, source_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ticket_id FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
        return row["ticket_id"] if row else None

    def rebind_sources(self, old_ticket_id: str, new_ticket_id: str) -> int:
        """合并后把挂在旧工单上的所有来源号改指主工单。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sources SET ticket_id=? WHERE ticket_id=?",
                (new_ticket_id, old_ticket_id),
            )
            return cur.rowcount

    # ---- 班组注册表 ----

    def upsert_crew(
        self,
        crew_id: str,
        name: str,
        region: str,
        categories: Iterable[str],
        active: bool = True,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO crews(crew_id, name, region, categories, active)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(crew_id) DO UPDATE SET
                     name=excluded.name, region=excluded.region,
                     categories=excluded.categories, active=excluded.active""",
                (crew_id, name, region, json.dumps(list(categories), ensure_ascii=False),
                 1 if active else 0),
            )

    def list_crews(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM crews"
        if active_only:
            sql += " WHERE active=1"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        result = []
        for r in rows:
            result.append(
                {
                    "crew_id": r["crew_id"],
                    "name": r["name"],
                    "region": r["region"],
                    "categories": json.loads(r["categories"]),
                    "active": bool(r["active"]),
                }
            )
        return result
