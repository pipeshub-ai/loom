"""Upgrading LOOM onto a database that already has data in it.

Both SQL stores bootstrapped with ``CREATE TABLE IF NOT EXISTS`` and nothing
else, which handles exactly one kind of change — a new table. A new *column* on
an existing table is a silent no-op, and then every ``INSERT`` naming it fails
with ``no such column``: a total, permanent write outage until a human runs DDL.

The cost was paid in advance rather than in production. It is why
``lease_expires_at`` and ``finished_at`` stayed buried in the JSON payload where
no index reaches them, which is why orphan reclamation and retention had to
filter in Python after a newest-first page — and so silently stopped finding the
oldest records, the only ones either of them wants.

These tests build a database at the *old* schema, put rows in it, and then open
it with the current store. That ordering is the point: a migration verified only
against an empty database has verified the easy half.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from conformance.backends import BY_NAME
from loom.core.models import ExecutionRecord, ExecutionStatus
from loom.stores import migrations
from loom.stores.sqlite import SCHEMA, SQLiteStore

#: The executions table as it was before migration 1 — no lifted columns.
OLD_SCHEMA = SCHEMA


@pytest.fixture
def legacy_db(tmp_path):
    """A database at the pre-migration schema, with a run already in it."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        """INSERT INTO executions
           (run_id, workflow, status, parent_run_id, idempotency_key, wake_at,
            awaiting_event, created_at, data)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "old-run",
            "legacy_flow",
            "completed",
            None,
            None,
            None,
            None,
            "2026-01-01T00:00:00+00:00",
            ExecutionRecord(
                run_id="old-run",
                workflow="legacy_flow",
                status=ExecutionStatus.COMPLETED,
            ).model_dump_json(),
        ),
    )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()
    return path


class TestSQLiteUpgrade:
    async def test_an_old_database_reports_version_zero(self, legacy_db) -> None:
        """Unmigrated and brand-new are the same state, and have to be.

        Every database written before migrations existed reads zero, so the
        steps must be safe on a fresh schema too.
        """
        conn = sqlite3.connect(legacy_db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        conn.close()

    async def test_opening_it_migrates_it(self, legacy_db) -> None:
        store = SQLiteStore(legacy_db)
        await store.create_execution(ExecutionRecord(run_id="new", workflow="w"))
        await store.close()

        conn = sqlite3.connect(legacy_db)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
        conn.close()

        assert version == migrations.LATEST
        assert {"lease_expires_at", "finished_at"} <= columns

    async def test_the_existing_row_survives(self, legacy_db) -> None:
        """A migration that loses data is worse than the gap it closes."""
        store = SQLiteStore(legacy_db)

        found = await store.get_execution("old-run")
        await store.close()

        assert found is not None
        assert found.workflow == "legacy_flow"
        assert found.status is ExecutionStatus.COMPLETED

    async def test_writes_work_after_the_upgrade(self, legacy_db) -> None:
        """The failure being prevented: an INSERT naming a column that is absent."""
        store = SQLiteStore(legacy_db)

        await store.create_execution(ExecutionRecord(run_id="after", workflow="w"))
        found = await store.get_execution("after")
        await store.close()

        assert found is not None

    async def test_migrating_twice_is_a_no_op(self, legacy_db) -> None:
        """Every step has to survive being applied to a database that has it.

        A crash between applying and recording must cost a repeat, not a
        corruption — and SQLite has no `ADD COLUMN IF NOT EXISTS`, so the
        idempotency is recovered from the error rather than declared.
        """
        first = SQLiteStore(legacy_db)
        await first.create_execution(ExecutionRecord(run_id="a", workflow="w"))
        await first.close()

        second = SQLiteStore(legacy_db)
        await second.create_execution(ExecutionRecord(run_id="b", workflow="w"))
        await second.close()

        conn = sqlite3.connect(legacy_db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        conn.close()

    async def test_a_replayed_step_is_not_re_applied_from_zero(self, tmp_path) -> None:
        """A database pinned mid-way applies only what is left."""
        path = tmp_path / "partial.db"
        conn = sqlite3.connect(path)
        conn.executescript(OLD_SCHEMA)
        conn.execute("ALTER TABLE executions ADD COLUMN lease_expires_at TEXT")
        conn.execute("ALTER TABLE executions ADD COLUMN finished_at TEXT")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        store = SQLiteStore(path)
        await store.create_execution(ExecutionRecord(run_id="x", workflow="w"))
        await store.close()

        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        conn.close()


class TestMigrationTable:
    def test_versions_are_unique_and_ordered(self) -> None:
        versions = [m.version for m in migrations.MIGRATIONS]

        assert versions == sorted(versions)
        assert len(versions) == len(set(versions))

    def test_latest_matches_the_table(self) -> None:
        assert max(m.version for m in migrations.MIGRATIONS) == migrations.LATEST

    def test_pending_returns_only_what_is_above_the_current_version(self) -> None:
        assert migrations.pending(migrations.LATEST, "sqlite") == []
        assert len(migrations.pending(0, "sqlite")) == len(
            [m for m in migrations.MIGRATIONS if m.sqlite]
        )

    def test_every_migration_covers_both_sql_dialects(self) -> None:
        """A step applied to one backend and not the other is a divergence.

        The conformance suite tests behaviour, not schema, so it would not
        notice — which is how Postgres ended up with a `kind` column SQLite
        never had.
        """
        missing = [
            m.version
            for m in migrations.MIGRATIONS
            if bool(m.sqlite) != bool(m.postgres)
        ]

        assert not missing, f"migrations defined for only one dialect: {missing}"


class TestPostgresUpgrade:
    @pytest.fixture
    async def legacy_pg(self):
        backend = BY_NAME["postgres"]
        reason = backend.why_not()
        if reason:
            pytest.skip(f"postgres: {reason}")
        import asyncpg

        schema = f"legacy_{uuid.uuid4().hex[:10]}"
        conn = await asyncpg.connect(backend.url)
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.close()
        yield backend.url, schema
        conn = await asyncpg.connect(backend.url)
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()

    async def test_the_version_table_is_created_and_advanced(self, legacy_pg) -> None:
        url, schema = legacy_pg
        import asyncpg

        conn = await asyncpg.connect(url, server_settings={"search_path": schema})
        try:
            from loom.stores.postgres import _SCHEMA

            await conn.execute(_SCHEMA)
            async with conn.transaction():
                reached = await migrations.apply_postgres(conn)
            assert reached == migrations.LATEST

            columns = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='executions' AND table_schema=$1",
                    schema,
                )
            }
            assert {"lease_expires_at", "finished_at"} <= columns

            # Idempotent: a second pass finds nothing to do.
            async with conn.transaction():
                assert await migrations.apply_postgres(conn) == migrations.LATEST
        finally:
            await conn.close()
