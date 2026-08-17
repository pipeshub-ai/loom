"""Forward-only schema migrations for the SQL stores.

Both SQL backends bootstrapped with ``CREATE TABLE IF NOT EXISTS`` and nothing
else, which handles exactly one kind of change: a new table. A new *column* on
an existing table is a silent no-op — the table keeps its old shape, and then
every ``INSERT`` naming the new column fails with ``no such column``. Not a
degraded read; a total, permanent write outage on that table until somebody runs
DDL by hand.

The cost of that gap was not hypothetical. It is why ``lease_expires_at`` and
``finished_at`` stayed inside the JSON payload where no index can reach them,
which in turn is why ``reclaim_orphans`` and ``RetentionManager.compact`` had to
filter in Python after a newest-first ``LIMIT`` — and so silently stopped
finding the very records they exist to find. The schema was designed never to
change, and the avoidance produced the bugs.

Design notes:

* **Forward-only, and numbered.** No down-migrations. Rolling back a deploy must
  not require rewriting data; the models tolerate columns they do not know about
  (see :mod:`loom.core.compat`), which is the cheaper half of the same problem.
* **Idempotent by construction.** Every step is written to survive being applied
  to a database that already has it, so a crash between applying and recording
  costs a repeat, not a corruption.
* **Serialised through the store's own lock.** Two processes starting at once
  must not both run DDL. Every store already implements ``LockProvider``, so
  there is no new dependency.
* **Version 0 means "before this existed"**, which is indistinguishable from a
  brand-new database — and has to be, because that is genuinely the state of
  every database written before this module. Steps therefore have to be safe on
  a fresh schema as well as an old one, which the ``IF NOT EXISTS`` forms give.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """One numbered, forward-only schema change."""

    version: int
    description: str
    sqlite: tuple[str, ...] = ()
    postgres: tuple[str, ...] = ()


#: Every migration, in order. Append only — never renumber, never edit a shipped
#: entry. A database records the highest version it has applied, so changing an
#: old one leaves already-migrated databases silently different from new ones.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description=(
            "lift lease_expires_at and finished_at out of the JSON payload so "
            "orphan reclamation and retention can index them"
        ),
        sqlite=(
            "ALTER TABLE executions ADD COLUMN lease_expires_at TEXT",
            "ALTER TABLE executions ADD COLUMN finished_at TEXT",
            "CREATE INDEX IF NOT EXISTS ix_executions_lease "
            "ON executions(status, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS ix_executions_finished "
            "ON executions(status, finished_at)",
        ),
        postgres=(
            "ALTER TABLE executions ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
            "ALTER TABLE executions ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_executions_lease "
            "ON executions(status, lease_expires_at) WHERE lease_expires_at IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_executions_finished "
            "ON executions(status, finished_at) WHERE finished_at IS NOT NULL",
        ),
    ),
    Migration(
        version=2,
        description="index event_deliveries.expires_at, swept on every event claim",
        sqlite=(
            "CREATE INDEX IF NOT EXISTS ix_event_deliveries_expiry "
            "ON event_deliveries(expires_at)",
        ),
        postgres=(
            "CREATE INDEX IF NOT EXISTS ix_event_deliveries_expiry "
            "ON event_deliveries(expires_at)",
        ),
    ),
    Migration(
        version=3,
        description="index executions.status alone, for the status-only listings",
        # ix_executions_workflow is (workflow, status), so it cannot serve a
        # query that gives only a status — which is what `loom runs --status
        # failed`, retention, and orphan reclamation all issue.
        sqlite=(
            "CREATE INDEX IF NOT EXISTS ix_executions_status ON executions(status, run_id)",
        ),
        postgres=(
            "CREATE INDEX IF NOT EXISTS ix_executions_status ON executions(status, run_id)",
        ),
    ),
)

#: What a fully-migrated database reports.
LATEST = max(m.version for m in MIGRATIONS)


def pending(current: int, dialect: str) -> Sequence[Migration]:
    """Migrations above *current*, in order."""
    return [m for m in MIGRATIONS if m.version > current and getattr(m, dialect)]


def _is_duplicate_column(exc: Exception) -> bool:
    """Whether *exc* means "this column is already here".

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so the idempotency this module
    promises has to be recovered from the error. Matched on the message because
    ``sqlite3.OperationalError`` carries no code.
    """
    return "duplicate column name" in str(exc).lower()


async def apply_sqlite(execute: Callable[[str], Any], read_version: Callable[[], int]) -> int:
    """Bring a SQLite database up to :data:`LATEST`. Returns the version reached.

    Uses ``PRAGMA user_version``, a 32-bit integer SQLite stores in the database
    header. No table to create, nothing to bootstrap, and it is already zero on
    every database written before this existed — which is the value that means
    "unmigrated", exactly as needed.
    """
    current = read_version()
    if current >= LATEST:
        return current

    for migration in pending(current, "sqlite"):
        for statement in migration.sqlite:
            try:
                execute(statement)
            except Exception as exc:
                if _is_duplicate_column(exc):
                    logger.debug("migration %d: %s", migration.version, exc)
                    continue
                raise
        # PRAGMA does not take a bound parameter, and the value is an int from
        # a frozen table in this module, never user input.
        execute(f"PRAGMA user_version = {migration.version}")
        logger.info("applied migration %d: %s", migration.version, migration.description)
    return LATEST


SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS loom_schema_version (
    id      INTEGER PRIMARY KEY,
    version INTEGER NOT NULL
);
"""


async def apply_postgres(conn: Any) -> int:
    """Bring a Postgres database up to :data:`LATEST`. Returns the version reached.

    Postgres has no ``user_version``, so the marker is a one-row table. Created
    here rather than in the main schema so a database that predates this module
    is handled by the same path as a fresh one.
    """
    await conn.execute(SCHEMA_VERSION_TABLE)
    row = await conn.fetchrow("SELECT version FROM loom_schema_version WHERE id = 1")
    current = int(row["version"]) if row else 0
    if current >= LATEST:
        return current

    for migration in pending(current, "postgres"):
        for statement in migration.postgres:
            await conn.execute(statement)
        await conn.execute(
            """INSERT INTO loom_schema_version (id, version) VALUES (1, $1)
               ON CONFLICT (id) DO UPDATE SET version = $1""",
            migration.version,
        )
        logger.info("applied migration %d: %s", migration.version, migration.description)
    return LATEST
