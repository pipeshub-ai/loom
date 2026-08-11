"""PostgreSQL store — implements ExecutionStore, CacheStore, LockProvider, TriggerStore.

Requires ``asyncpg``: ``pip install workflow-builder[postgres]``

Usage::

    from workflow_builder.state.postgres import PostgresStore

    store = PostgresStore("postgresql://user:pass@localhost/workflows")
    await store.connect()
    rt = Runtime(store=store)
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from workflow_builder.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from workflow_builder.runtime.journal import JournalEntry, path_order

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS executions (
    run_id           TEXT PRIMARY KEY,
    workflow         TEXT NOT NULL,
    status           TEXT NOT NULL,
    parent_run_id    TEXT,
    idempotency_key  TEXT UNIQUE,
    wake_at          TIMESTAMPTZ,
    awaiting_event   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data             JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exec_workflow
    ON executions(workflow, status);
CREATE INDEX IF NOT EXISTS ix_exec_wake
    ON executions(status, wake_at)
    WHERE wake_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_exec_event
    ON executions(status, awaiting_event)
    WHERE awaiting_event IS NOT NULL;

CREATE TABLE IF NOT EXISTS journal (
    run_id     TEXT NOT NULL,
    path       TEXT NOT NULL,
    sort_key   TEXT NOT NULL,
    data       JSONB NOT NULL,
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS ix_journal_order
    ON journal(run_id, sort_key);

CREATE TABLE IF NOT EXISTS events (
    id       BIGSERIAL PRIMARY KEY,
    run_id   TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL,
    data     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_target
    ON events(run_id, name);

CREATE TABLE IF NOT EXISTS triggers (
    trigger_id   TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    next_fire_at TIMESTAMPTZ,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    data         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_triggers_due
    ON triggers(next_fire_at)
    WHERE enabled AND next_fire_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    expires_at DOUBLE PRECISION NOT NULL,
    value      JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    key        TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
"""


class PostgresStore:
    """PostgreSQL-backed store using asyncpg.

    Parameters
    ----------
    dsn:
        PostgreSQL connection string.
    pool_size:
        Connection pool size (default 10).
    """

    def __init__(
        self,
        dsn: str = "postgresql://localhost/workflow_builder",
        *,
        pool_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._pool_size = pool_size
        self._pool: Any = None

    async def connect(self) -> None:
        """Create pool and ensure schema exists."""
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=2, max_size=self._pool_size
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()

    # ------------------------------------------------------------------
    # ExecutionStore
    # ------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None:
        data = record.model_dump_json()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO executions
                   (run_id, workflow, status, parent_run_id,
                    idempotency_key, wake_at, awaiting_event,
                    created_at, data)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (run_id) DO UPDATE
                   SET data=$9, status=$3, wake_at=$6,
                       awaiting_event=$7""",
                record.run_id,
                record.workflow,
                record.status.value,
                record.parent_run_id,
                record.idempotency_key,
                record.wake_at,
                record.awaiting_event,
                record.created_at or datetime.now(UTC),
                data,
            )

    async def get_execution(
        self, run_id: str
    ) -> ExecutionRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM executions WHERE run_id=$1",
                run_id,
            )
        if row is None:
            return None
        return ExecutionRecord.model_validate_json(row["data"])

    async def update_execution(self, record: ExecutionRecord) -> None:
        await self.create_execution(record)

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
        conditions = []
        params: list[Any] = []
        idx = 1

        if workflow:
            conditions.append(f"workflow=${idx}")
            params.append(workflow)
            idx += 1
        if status:
            conditions.append(f"status=${idx}")
            params.append(status.value)
            idx += 1

        where = " AND ".join(conditions) if conditions else "TRUE"
        query = (
            f"SELECT data FROM executions WHERE {where} "
            f"ORDER BY run_id DESC LIMIT ${idx} OFFSET ${idx+1}"
        )
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        results = [
            ExecutionRecord.model_validate_json(r["data"])
            for r in rows
        ]

        # Apply tag/metadata filters in Python (JSONB queries possible
        # but kept simple for parity with other stores)
        if tags:
            tag_set = set(tags)
            results = [
                r for r in results
                if tag_set.issubset(set(r.tags))
            ]
        if metadata:
            results = [
                r for r in results
                if all(r.metadata.get(k) == v for k, v in metadata.items())
            ]
        return results

    async def find_by_idempotency_key(
        self, key: str
    ) -> ExecutionRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM executions WHERE idempotency_key=$1",
                key,
            )
        if row is None:
            return None
        return ExecutionRecord.model_validate_json(row["data"])

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    async def save_journal(
        self, run_id: str, entries: list[JournalEntry]
    ) -> None:
        if not entries:
            return
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO journal (run_id, path, sort_key, data)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (run_id, path) DO UPDATE
                   SET sort_key=$3, data=$4""",
                [
                    (
                        run_id,
                        e.path,
                        ".".join(
                            str(s).zfill(9)
                            for s in path_order(e.path)
                        ),
                        e.model_dump_json(),
                    )
                    for e in entries
                ],
            )

    async def load_journal(
        self, run_id: str
    ) -> list[JournalEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT data FROM journal
                   WHERE run_id=$1 ORDER BY sort_key""",
                run_id,
            )
        return [
            JournalEntry.model_validate_json(r["data"])
            for r in rows
        ]

    async def truncate_journal(
        self, run_id: str, from_path: str
    ) -> None:
        from_key = ".".join(
            str(s).zfill(9) for s in path_order(from_path)
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                """DELETE FROM journal
                   WHERE run_id=$1 AND sort_key >= $2""",
                run_id,
                from_key,
            )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO events (run_id, name, data)
                   VALUES ($1, $2, $3)""",
                event.run_id or "",
                event.name,
                event.model_dump_json(),
            )

    async def take_event(
        self, run_id: str, name: str
    ) -> Event | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """DELETE FROM events
                   WHERE id = (
                       SELECT id FROM events
                       WHERE name=$1 AND run_id IN ($2, '')
                       ORDER BY (run_id = '') ASC, id ASC
                       LIMIT 1
                   )
                   RETURNING data""",
                name,
                run_id,
            )
        if row is None:
            return None
        return Event.model_validate_json(row["data"])

    async def runs_awaiting_event(
        self, name: str
    ) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT run_id FROM executions
                   WHERE status=$1 AND awaiting_event=$2""",
                ExecutionStatus.SUSPENDED.value,
                name,
            )
        return [r["run_id"] for r in rows]

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    async def due_runs(
        self, now: datetime, *, limit: int = 100
    ) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT run_id FROM executions
                   WHERE status=$1 AND wake_at <= $2
                   ORDER BY wake_at LIMIT $3""",
                ExecutionStatus.SUSPENDED.value,
                now,
                limit,
            )
        return [r["run_id"] for r in rows]

    # ------------------------------------------------------------------
    # CacheStore
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT expires_at, value FROM cache WHERE key=$1",
                key,
            )
        if row is None:
            return None
        if row["expires_at"] < time.time():
            await self.delete(key)
            return None
        return json.loads(row["value"])

    async def set(
        self, key: str, value: Any, ttl_seconds: float
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO cache (key, expires_at, value)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (key) DO UPDATE
                   SET expires_at=$2, value=$3""",
                key,
                time.time() + ttl_seconds,
                json.dumps(value),
            )

    async def delete(self, key: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cache WHERE key=$1", key
            )

    # ------------------------------------------------------------------
    # LockProvider
    # ------------------------------------------------------------------

    async def acquire(
        self, key: str, owner: str, ttl_seconds: float
    ) -> bool:
        now = time.time()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO locks (key, owner, expires_at)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (key) DO UPDATE
                   SET owner=$2, expires_at=$3
                   WHERE locks.owner=$2 OR locks.expires_at < $4
                   RETURNING owner""",
                key,
                owner,
                now + ttl_seconds,
                now,
            )
        return row is not None and row["owner"] == owner

    async def renew(
        self, key: str, owner: str, ttl_seconds: float
    ) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE locks SET expires_at=$3
                   WHERE key=$1 AND owner=$2""",
                key,
                owner,
                time.time() + ttl_seconds,
            )
        return result.endswith("1")

    async def release(self, key: str, owner: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM locks WHERE key=$1 AND owner=$2",
                key,
                owner,
            )

    # ------------------------------------------------------------------
    # TriggerStore
    # ------------------------------------------------------------------

    async def save_trigger(self, trigger: TriggerRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO triggers
                   (trigger_id, workflow, kind, next_fire_at,
                    enabled, data)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (trigger_id) DO UPDATE
                   SET next_fire_at=$4, enabled=$5, data=$6""",
                trigger.trigger_id,
                trigger.workflow,
                trigger.kind.value,
                trigger.next_fire_at,
                trigger.enabled,
                trigger.model_dump_json(),
            )

    async def get_trigger(
        self, trigger_id: str
    ) -> TriggerRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM triggers WHERE trigger_id=$1",
                trigger_id,
            )
        if row is None:
            return None
        return TriggerRecord.model_validate_json(row["data"])

    async def list_triggers(
        self, *, workflow: str | None = None
    ) -> list[TriggerRecord]:
        query = "SELECT data FROM triggers"
        params: list[Any] = []
        if workflow:
            query += " WHERE workflow=$1"
            params.append(workflow)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [
            TriggerRecord.model_validate_json(r["data"])
            for r in rows
        ]

    async def due_triggers(
        self, now: datetime, *, limit: int = 50
    ) -> list[TriggerRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT data FROM triggers
                   WHERE enabled AND next_fire_at <= $1
                   ORDER BY next_fire_at
                   LIMIT $2""",
                now,
                limit,
            )
        return [
            TriggerRecord.model_validate_json(r["data"])
            for r in rows
        ]

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None:
        # Atomic update — no read-modify-write race
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE triggers SET
                   next_fire_at = $2,
                   data = jsonb_set(
                       jsonb_set(
                           jsonb_set(data, '{last_fire_at}',
                               to_jsonb($3::text)),
                           '{next_fire_at}',
                           CASE WHEN $2 IS NULL THEN 'null'::jsonb
                                ELSE to_jsonb($2::text) END),
                       '{run_count}',
                       to_jsonb((data->>'run_count')::int + 1))
                   WHERE trigger_id = $1""",
                trigger_id,
                next_fire,
                last_fire.isoformat(),
            )

    async def delete_trigger(self, trigger_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM triggers WHERE trigger_id=$1",
                trigger_id,
            )
