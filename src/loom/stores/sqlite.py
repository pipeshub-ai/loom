"""SQLite-backed store: durable execution on a laptop, with no server to run.

Uses the standard library ``sqlite3`` behind :func:`asyncio.to_thread`, so there is no
extra dependency. WAL mode is enabled so a worker process and a dev server can share one
file without blocking each other.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loom.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from loom.runtime.journal import JournalEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    run_id           TEXT PRIMARY KEY,
    workflow         TEXT NOT NULL,
    status           TEXT NOT NULL,
    parent_run_id    TEXT,
    idempotency_key  TEXT UNIQUE,
    wake_at          TEXT,
    awaiting_event   TEXT,
    created_at       TEXT,
    data             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_executions_workflow ON executions(workflow, status);
CREATE INDEX IF NOT EXISTS ix_executions_wake ON executions(status, wake_at);
CREATE INDEX IF NOT EXISTS ix_executions_event ON executions(status, awaiting_event);

CREATE TABLE IF NOT EXISTS journal (
    run_id     TEXT NOT NULL,
    path       TEXT NOT NULL,
    sort_key   TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS ix_journal_order ON journal(run_id, sort_key);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_target ON events(run_id, name);

CREATE TABLE IF NOT EXISTS triggers (
    trigger_id   TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    next_fire_at TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_triggers_due ON triggers(enabled, next_fire_at);

CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    expires_at REAL,          -- NULL means never expires
    value      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    key        TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


def _sort_key(path: str) -> str:
    """Zero-pad each path segment so lexicographic SQL ordering matches numeric order."""
    return ".".join(segment.rjust(9, "0") for segment in path.split("."))


class SQLiteStore:
    """Implements :class:`ExecutionStore`, :class:`CacheStore`, and :class:`LockProvider`."""

    def __init__(self, path: str | Path = "workflow.db") -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None
        self._mutex = asyncio.Lock()

    # -- connection -------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(SCHEMA)
            connection.commit()
            self._connection = connection
        return self._connection

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        def run() -> None:
            connection = self._connect()
            connection.execute(sql, params)
            connection.commit()

        async with self._mutex:
            await asyncio.to_thread(run)

    async def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            return list(self._connect().execute(sql, params).fetchall())

        async with self._mutex:
            return await asyncio.to_thread(run)

    async def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- executions -------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None:
        await self._execute(
            """INSERT OR REPLACE INTO executions
               (run_id, workflow, status, parent_run_id, idempotency_key, wake_at,
                awaiting_event, created_at, data)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            self._row(record),
        )

    async def update_execution(self, record: ExecutionRecord) -> None:
        await self.create_execution(record)

    @staticmethod
    def _row(record: ExecutionRecord) -> tuple[Any, ...]:
        return (
            record.run_id,
            record.workflow,
            record.status.value,
            record.parent_run_id,
            record.idempotency_key,
            record.wake_at.isoformat() if record.wake_at else None,
            record.awaiting_event,
            (record.created_at or datetime.now(UTC)).isoformat(),
            record.model_dump_json(),
        )

    async def get_execution(self, run_id: str) -> ExecutionRecord | None:
        rows = await self._query("SELECT data FROM executions WHERE run_id = ?", (run_id,))
        return ExecutionRecord.model_validate_json(rows[0]["data"]) if rows else None

    async def list_executions(
        self,
        *,
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if workflow is not None:
            clauses.append("workflow = ?")
            params.append(workflow)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._query(
            f"SELECT data FROM executions {where} ORDER BY run_id DESC LIMIT ? OFFSET ?",
            (*params, limit if not (tags or metadata) else limit * 10, offset),
        )
        records = [ExecutionRecord.model_validate_json(row["data"]) for row in rows]

        # Tag and metadata predicates are applied in Python: a JSON1 query would be
        # faster but would not port to every backend that reuses this schema.
        if tags:
            records = [r for r in records if set(tags).issubset(r.tags)]
        if metadata:
            records = [
                r for r in records if all(r.metadata.get(k) == v for k, v in metadata.items())
            ]
        return records[:limit]

    async def find_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        rows = await self._query("SELECT data FROM executions WHERE idempotency_key = ?", (key,))
        return ExecutionRecord.model_validate_json(rows[0]["data"]) if rows else None

    async def delete_execution(self, run_id: str) -> None:
        await self._execute("DELETE FROM journal WHERE run_id = ?", (run_id,))
        await self._execute("DELETE FROM executions WHERE run_id = ?", (run_id,))

    # -- triggers ---------------------------------------------------------------------
    #
    # SQLite carried none of these, and ``TriggerDispatcher`` fell back to an
    # in-memory store for any store that lacked ``save_trigger``. So the
    # documented laptop default persisted runs durably and kept schedules in
    # memory: a restart lost every cron trigger, with no error and no log line.

    async def save_trigger(self, trigger: TriggerRecord) -> None:
        await self._execute(
            """INSERT OR REPLACE INTO triggers
               (trigger_id, workflow, next_fire_at, enabled, data)
               VALUES (?,?,?,?,?)""",
            (
                trigger.trigger_id,
                trigger.workflow,
                trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
                1 if trigger.enabled else 0,
                trigger.model_dump_json(),
            ),
        )

    async def get_trigger(self, trigger_id: str) -> TriggerRecord | None:
        rows = await self._query(
            "SELECT data FROM triggers WHERE trigger_id = ?", (trigger_id,)
        )
        return TriggerRecord.model_validate_json(rows[0]["data"]) if rows else None

    async def list_triggers(
        self, *, workflow: str | None = None
    ) -> list[TriggerRecord]:
        sql = "SELECT data FROM triggers"
        params: tuple[Any, ...] = ()
        if workflow is not None:
            sql += " WHERE workflow = ?"
            params = (workflow,)
        rows = await self._query(sql + " ORDER BY trigger_id", params)
        return [TriggerRecord.model_validate_json(row["data"]) for row in rows]

    async def due_triggers(
        self, now: datetime, *, limit: int = 50
    ) -> list[TriggerRecord]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        rows = await self._query(
            """SELECT data FROM triggers
               WHERE enabled = 1 AND next_fire_at IS NOT NULL
                 AND next_fire_at <= ?
               ORDER BY next_fire_at LIMIT ?""",
            (now.isoformat(), limit),
        )
        return [TriggerRecord.model_validate_json(row["data"]) for row in rows]

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None:
        current = await self.get_trigger(trigger_id)
        if current is None:
            return
        await self.save_trigger(
            current.model_copy(
                update={
                    "last_fire_at": last_fire,
                    "next_fire_at": next_fire,
                    "run_count": current.run_count + 1,
                }
            )
        )

    async def delete_trigger(self, trigger_id: str) -> None:
        await self._execute("DELETE FROM triggers WHERE trigger_id = ?", (trigger_id,))

    # -- journals ---------------------------------------------------------------------

    async def save_journal(self, run_id: str, entries: list[JournalEntry]) -> None:
        if not entries:
            return

        payload = [
            (run_id, entry.path, _sort_key(entry.path), entry.model_dump_json())
            for entry in entries
        ]

        def run() -> None:
            connection = self._connect()
            connection.executemany(
                "INSERT OR REPLACE INTO journal (run_id, path, sort_key, data) VALUES (?,?,?,?)",
                payload,
            )
            connection.commit()

        async with self._mutex:
            await asyncio.to_thread(run)

    async def load_journal(self, run_id: str) -> list[JournalEntry]:
        rows = await self._query(
            "SELECT data FROM journal WHERE run_id = ? ORDER BY sort_key", (run_id,)
        )
        return [JournalEntry.model_validate_json(row["data"]) for row in rows]

    async def truncate_journal(self, run_id: str, from_path: str) -> None:
        await self._execute(
            "DELETE FROM journal WHERE run_id = ? AND sort_key >= ?",
            (run_id, _sort_key(from_path)),
        )

    # -- events -----------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None:
        event.received_at = event.received_at or datetime.now(UTC)
        await self._execute(
            "INSERT INTO events (run_id, name, data) VALUES (?,?,?)",
            (event.run_id or "", event.name, event.model_dump_json()),
        )

    async def take_event(self, run_id: str, name: str) -> Event | None:
        def run() -> Event | None:
            connection = self._connect()
            row = connection.execute(
                """SELECT id, data FROM events
                   WHERE name = ? AND run_id IN (?, '')
                   ORDER BY (run_id = '') ASC, id ASC LIMIT 1""",
                (name, run_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM events WHERE id = ?", (row["id"],))
            connection.commit()
            return Event.model_validate_json(row["data"])

        async with self._mutex:
            return await asyncio.to_thread(run)

    async def runs_awaiting_event(self, name: str) -> list[str]:
        rows = await self._query(
            "SELECT run_id FROM executions WHERE status = ? AND awaiting_event = ?",
            (ExecutionStatus.SUSPENDED.value, name),
        )
        return [row["run_id"] for row in rows]

    # -- timers -----------------------------------------------------------------------

    async def due_runs(self, now: datetime, *, limit: int = 100) -> list[str]:
        rows = await self._query(
            """SELECT run_id FROM executions
               WHERE status = ? AND wake_at IS NOT NULL AND wake_at <= ?
               ORDER BY wake_at LIMIT ?""",
            (ExecutionStatus.SUSPENDED.value, now.isoformat(), limit),
        )
        return [row["run_id"] for row in rows]

    # -- cache ------------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        rows = await self._query("SELECT expires_at, value FROM cache WHERE key = ?", (key,))
        if not rows:
            return None
        expires_at = rows[0]["expires_at"]
        # NULL means the entry never goes stale — see CacheStore.set.
        if expires_at is not None and expires_at < time.time():
            await self.delete(key)
            return None
        return json.loads(rows[0]["value"])

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO cache (key, expires_at, value) VALUES (?,?,?)",
            (
                key,
                time.time() + ttl_seconds if ttl_seconds > 0 else None,
                json.dumps(value),
            ),
        )

    async def delete(self, key: str) -> None:
        await self._execute("DELETE FROM cache WHERE key = ?", (key,))

    # -- locks ------------------------------------------------------------------------

    async def acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        def run() -> bool:
            connection = self._connect()
            now = time.time()
            with connection:
                row = connection.execute(
                    "SELECT owner, expires_at FROM locks WHERE key = ?", (key,)
                ).fetchone()
                if row is not None and row["owner"] != owner and row["expires_at"] > now:
                    return False
                connection.execute(
                    "INSERT OR REPLACE INTO locks (key, owner, expires_at) VALUES (?,?,?)",
                    (key, owner, now + ttl_seconds),
                )
            return True

        async with self._mutex:
            return await asyncio.to_thread(run)

    async def renew(self, key: str, owner: str, ttl_seconds: float) -> bool:
        return await self.acquire(key, owner, ttl_seconds)

    async def release(self, key: str, owner: str) -> None:
        await self._execute("DELETE FROM locks WHERE key = ? AND owner = ?", (key, owner))
