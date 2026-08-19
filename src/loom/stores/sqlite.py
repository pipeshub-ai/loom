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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loom.core.exceptions import ConcurrentUpdateError, ConfigurationError
from loom.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from loom.runtime.journal import JournalEntry
from loom.stores.base import utc_iso

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

CREATE TABLE IF NOT EXISTS event_deliveries (
    key        TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
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
            try:
                connection = sqlite3.connect(self.path, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(SCHEMA)
                connection.commit()
                self._migrate(connection)
            except sqlite3.OperationalError as exc:
                # Covers the whole opening, not just connect(): a directory that
                # is readable and not writable connects fine and fails on the
                # first PRAGMA, which is the same problem one line later.
                raise ConfigurationError(self._unopenable(exc)) from exc
            self._connection = connection
        return self._connection

    def _unopenable(self, exc: sqlite3.OperationalError) -> str:
        """Say which file could not be opened, and why the URL may be the reason.

        ``sqlite3`` reports "unable to open database file" and not which file,
        so a URL typo surfaces as a stack trace ending in a sentence that names
        nothing. The common typo has one cause: ``sqlite:///runs.db`` is three
        slashes, which is an *absolute* path to ``/runs.db`` at the filesystem
        root -- a directory nobody can write to. The relative spelling this
        store wants keeps the name in the URL's authority position, with two.
        """
        detail = f"cannot open SQLite database at {Path(self.path).absolute()}: {exc}"
        if Path(self.path).parent == Path("/") and self.path.startswith("/"):
            return (
                f"{detail}\n"
                "  A store URL of 'sqlite:///name.db' resolves to '/name.db' at the "
                "filesystem root.\n"
                "  For a file beside you, use two slashes: sqlite://name.db\n"
                "  For an absolute path, give it in full: sqlite:///var/lib/loom/name.db"
            )
        return detail

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Apply pending migrations under SQLite's own write lock.

        Synchronous because it runs inside ``_connect``, which is already on a
        worker thread. ``BEGIN IMMEDIATE`` is what keeps two processes opening
        the same file at once from both running DDL — the file lock is the only
        cross-process primitive available before the store is usable.
        """
        from loom.stores import migrations

        connection.execute("BEGIN IMMEDIATE")
        try:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current >= migrations.LATEST:
                connection.execute("ROLLBACK")
                return
            for migration in migrations.pending(current, "sqlite"):
                for statement in migration.sqlite:
                    try:
                        connection.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if not migrations._is_duplicate_column(exc):
                            raise
                connection.execute(f"PRAGMA user_version = {migration.version}")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.commit()

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        def run() -> None:
            connection = self._connect()
            connection.execute(sql, params)
            connection.commit()

        async with self._mutex:
            await asyncio.to_thread(run)

    async def _execute_rowcount(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        def run() -> int:
            connection = self._connect()
            cursor = connection.execute(sql, params)
            connection.commit()
            return cursor.rowcount

        async with self._mutex:
            return await asyncio.to_thread(run)

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
                awaiting_event, created_at, lease_expires_at, finished_at, data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            self._row(record),
        )

    async def update_execution(
        self, record: ExecutionRecord, *, expected_status: ExecutionStatus | None = None
    ) -> None:
        if expected_status is None:
            await self.create_execution(record)
            return
        row = self._row(record)
        # UPDATE ... WHERE status=? is atomic under SQLite's own file-level
        # locking, so a concurrent resume from a second process either sees
        # rowcount==0 (lost the race) or 1 (won it) with no interleaving.
        rowcount = await self._execute_rowcount(
            """UPDATE executions
               SET workflow=?, status=?, parent_run_id=?, idempotency_key=?,
                   wake_at=?, awaiting_event=?, created_at=?, lease_expires_at=?,
                   finished_at=?, data=?
               WHERE run_id=? AND status=?""",
            (*row[1:], row[0], expected_status.value),
        )
        if rowcount == 0:
            current = await self.get_execution(record.run_id)
            raise ConcurrentUpdateError(
                record.run_id,
                expected=expected_status.value,
                actual=current.status.value if current is not None else None,
            )

    @staticmethod
    def _row(record: ExecutionRecord) -> tuple[Any, ...]:
        return (
            record.run_id,
            record.workflow,
            record.status.value,
            record.parent_run_id,
            record.idempotency_key,
            # utc_iso, not isoformat: this column is compared as TEXT by
            # due_runs, so a non-UTC offset would sort as a later string than
            # the instant it names. See loom.stores.base.utc_iso.
            utc_iso(record.wake_at),
            record.awaiting_event,
            utc_iso(record.created_at or datetime.now(UTC)),
            # Lifted out of the JSON payload by migration 1. They live in
            # columns because `reclaim_orphans` and retention filter on them,
            # and a predicate inside `data` cannot be a WHERE clause — which is
            # what forced both to scan a newest-first page and silently miss the
            # oldest records, the exact ones they exist to find.
            utc_iso(record.lease_expires_at),
            utc_iso(record.finished_at),
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
                utc_iso(trigger.next_fire_at),
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
            (utc_iso(now), limit),
        )
        return [TriggerRecord.model_validate_json(row["data"]) for row in rows]

    async def claim_due_triggers(
        self,
        now: datetime,
        *,
        owner: str,
        lease_seconds: float = 60.0,
        limit: int = 50,
    ) -> list[TriggerRecord]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        until = now + timedelta(seconds=lease_seconds)

        def run() -> list[TriggerRecord]:
            connection = self._connect()
            # BEGIN IMMEDIATE takes the write lock up front. Without it SQLite
            # starts a deferred read transaction and two callers can both read
            # the same due rows before either writes — the exact race, just
            # harder to see because SQLite serialises the writes afterwards.
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """SELECT data FROM triggers
                       WHERE enabled = 1 AND next_fire_at IS NOT NULL
                         AND next_fire_at <= ?
                       ORDER BY next_fire_at LIMIT ?""",
                    (utc_iso(now), limit),
                ).fetchall()
                won: list[TriggerRecord] = []
                for row in rows:
                    trigger = TriggerRecord.model_validate_json(row["data"])
                    held = trigger.claimed_until
                    if held is not None:
                        if held.tzinfo is None:
                            held = held.replace(tzinfo=UTC)
                        if held > now:
                            continue
                    claimed = trigger.model_copy(
                        update={"claimed_by": owner, "claimed_until": until}
                    )
                    connection.execute(
                        "UPDATE triggers SET data = ? WHERE trigger_id = ?",
                        (claimed.model_dump_json(), claimed.trigger_id),
                    )
                    won.append(claimed)
                connection.commit()
                return won
            except BaseException:
                connection.rollback()
                raise

        async with self._mutex:
            return await asyncio.to_thread(run)

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
                    # Release the claim; see MemoryStore for why at advance
                    # time rather than at expiry.
                    "claimed_by": "",
                    "claimed_until": None,
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

    async def claim_event_delivery(
        self, key: str, *, ttl_seconds: float = 604800.0
    ) -> bool:
        def run() -> bool:
            connection = self._connect()
            now = time.time()
            with connection:
                connection.execute(
                    "DELETE FROM event_deliveries WHERE expires_at <= ?", (now,)
                )
                # INSERT OR IGNORE against a PRIMARY KEY: the claim is the
                # insert, so two concurrent callers cannot both see it free.
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO event_deliveries (key, expires_at) "
                    "VALUES (?,?)",
                    (key, now + ttl_seconds),
                )
            return cursor.rowcount == 1

        # Under self._mutex, like every other write here. One connection is
        # shared across the thread pool, so reading `cursor.rowcount` from one
        # thread while another executes on the same connection interleaves —
        # 13 of 16 concurrent claimers "won" without it.
        async with self._mutex:
            return await asyncio.to_thread(run)

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
            (ExecutionStatus.SUSPENDED.value, utc_iso(now), limit),
        )
        return [row["run_id"] for row in rows]

    async def due_leases(
        self,
        before: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        rows = await self._query(
            f"""SELECT data FROM executions
                WHERE status IN ({marks})
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                ORDER BY lease_expires_at LIMIT ?""",
            (*[s.value for s in statuses], utc_iso(before), limit),
        )
        return [ExecutionRecord.model_validate_json(row["data"]) for row in rows]

    async def terminal_before(
        self,
        cutoff: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        rows = await self._query(
            f"""SELECT data FROM executions
                WHERE status IN ({marks})
                  AND finished_at IS NOT NULL AND finished_at < ?
                ORDER BY finished_at LIMIT ?""",
            (*[s.value for s in statuses], utc_iso(cutoff), limit),
        )
        return [ExecutionRecord.model_validate_json(row["data"]) for row in rows]

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
