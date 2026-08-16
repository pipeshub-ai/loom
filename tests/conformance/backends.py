"""Which stores the conformance suite runs against, and how it reaches them.

Every store in ``loom.stores`` declares the same four protocols. Until this
existed, the suite ran against ``memory`` and ``sqlite`` only and the other two
were covered by a ``hasattr`` check — so nothing asserted that Postgres orders
``list_executions`` the way SQLite does, or that Mongo's idempotency lookup is
actually unique, or that ``acquire()`` is atomic anywhere.

That is the bug class this exists for: a feature written against dict semantics
that behaves differently once rows go through SQL or documents through BSON.
Four stores that each pass their own tests and disagree with each other is four
subtly different products.

**A backend that cannot be reached is SKIPPED and named, never silently
dropped.** A suite that shrinks quietly when a service is down reports green for
coverage it did not have — the same rule the verification pipeline applies when
a linter is missing.

    LOOM_TEST_MONGO=mongodb://localhost:27017
    LOOM_TEST_POSTGRES=postgresql://postgres:loom@localhost:5432/loom

Both default to localhost. ``LOOM_TEST_STORES=memory,sqlite`` narrows the matrix
for a fast local loop; CI leaves it unset so everything runs.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import pytest

__all__ = ["ALL_BACKENDS", "Backend", "store_param", "unreachable"]

DEFAULT_MONGO = "mongodb://localhost:27017"
DEFAULT_POSTGRES = "postgresql://postgres:loom@localhost:5432/loom"


@dataclass(frozen=True)
class Backend:
    """One store implementation, and what it takes to talk to it."""

    name: str
    driver: str = ""
    """Import name of the driver, when one is needed."""
    env: str = ""
    """Environment variable naming the server, when one is needed."""
    default_url: str = ""

    @property
    def url(self) -> str:
        return os.environ.get(self.env, self.default_url) if self.env else ""

    def why_not(self) -> str:
        """Why this backend cannot run here, or ``""`` when it can.

        Returns a reason rather than a bool so the skip message says which of
        the three things is missing — a skip that only says "skipped" makes an
        absent driver and a down server look the same.
        """
        # Narrowing excludes; it does not exempt. Returning "OK" for a named
        # backend before checking the driver and the server would turn
        # LOOM_TEST_STORES=mongo with no mongo running into a connection error
        # instead of a skip — which is the opposite of what naming it meant.
        if _narrowing() and self.name not in _narrowed():
            return f"LOOM_TEST_STORES excludes {self.name!r}"
        if self.driver:
            try:
                __import__(self.driver)
            except ImportError:
                return f"driver {self.driver!r} is not installed"
        if self.env and unreachable(self.url):
            return f"no server at {self.url} (set ${self.env})"
        return ""


def _narrowing() -> bool:
    return bool(os.environ.get("LOOM_TEST_STORES"))


def _narrowed() -> set[str]:
    raw = os.environ.get("LOOM_TEST_STORES", "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def unreachable(url: str) -> bool:
    """True when nothing is listening. A short timeout: this runs per session."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (27017 if parsed.scheme.startswith("mongodb") else 5432)
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


#: The matrix. Adding a fifth store is adding a row here and a factory below —
#: which is the point: coverage should not be something each store opts into.
ALL_BACKENDS: tuple[Backend, ...] = (
    Backend("memory"),
    Backend("sqlite"),
    Backend("postgres", driver="asyncpg", env="LOOM_TEST_POSTGRES",
            default_url=DEFAULT_POSTGRES),
    Backend("mongo", driver="motor", env="LOOM_TEST_MONGO", default_url=DEFAULT_MONGO),
)

BY_NAME = {backend.name: backend for backend in ALL_BACKENDS}


@asynccontextmanager
async def open_store(name: str) -> AsyncIterator[Any]:
    """A fresh, isolated store of the named kind.

    Isolation is per-store rather than per-suite: a Mongo database and a
    Postgres schema per test, dropped afterwards. Sharing one namespace makes
    tests order-dependent, and an order-dependent conformance suite is worse
    than none — it goes green for the wrong reason.
    """
    backend = BY_NAME[name]
    reason = backend.why_not()
    if reason:
        pytest.skip(f"{name}: {reason}")

    if name == "memory":
        from loom.stores.memory import MemoryStore

        yield MemoryStore()
        return

    if name == "sqlite":
        from loom.stores.sqlite import SQLiteStore

        made = SQLiteStore(":memory:")
        try:
            yield made
        finally:
            await _close(made)
        return

    if name == "postgres":
        async for store in _postgres(backend.url):
            yield store
        return

    if name == "mongo":
        async for store in _mongo(backend.url):
            yield store
        return

    raise AssertionError(f"no factory for backend {name!r}")


async def _postgres(dsn: str) -> AsyncIterator[Any]:
    """A throwaway schema, so tables are real and the teardown is one DROP."""
    import asyncpg

    from loom.stores.postgres import PostgresStore

    schema = f"loom_t_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()

    made = PostgresStore(f"{dsn}?options=-csearch_path%3D{schema}")
    try:
        await made.connect()
        yield made
    finally:
        await _close(made)
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()


async def _mongo(uri: str) -> AsyncIterator[Any]:
    from loom.stores.mongo import MongoStore

    database = f"loom_t_{uuid.uuid4().hex[:12]}"
    made = MongoStore(uri, database=database)
    try:
        await made.ensure_indexes()
        yield made
    finally:
        try:
            await made._client.drop_database(database)
        finally:
            await _close(made)


async def _close(store: Any) -> None:
    close: Callable[..., Any] | None = getattr(store, "close", None)
    if close is not None:
        outcome = close()
        if hasattr(outcome, "__await__"):
            await outcome


def store_param() -> Any:
    """``pytest.mark.parametrize`` argument covering the whole matrix.

    Every backend is always a *case*, even when it cannot run. That is
    deliberate: an unreachable Postgres appears in the report as a named skip
    rather than vanishing, so "4 passed" and "2 passed, 2 skipped" are
    distinguishable at a glance.
    """
    return pytest.mark.parametrize(
        "store_name", [backend.name for backend in ALL_BACKENDS]
    )
