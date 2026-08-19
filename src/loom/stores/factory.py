"""Build a store from a URL, so persistence is a deployment decision.

Which store a workflow runs against is not the workflow's business. A workflow
declares steps and orchestration; where its journal lives is chosen by whoever
deploys it — tests want memory, a laptop wants SQLite, production wants Postgres,
and the *same workflow code* should run against all three.

That is why this lives here rather than in generated code: a URL is something an
environment can set, and a hardcoded ``MemoryStore()`` is not.

    LOOM_STORE=postgres://user:pw@db/loom   python -m my_app
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from loom.core.exceptions import ConfigurationError

#: Environment variable read by :func:`store_from_env` and ``Runtime.from_env``.
STORE_URL_ENV = "LOOM_STORE"

#: Used when nothing is configured. In-process and durable only for the life of
#: the process — right for tests and a first run, wrong for anything real.
DEFAULT_STORE_URL = "memory://"


def from_url(url: str) -> Any:
    """Construct a store from a URL.

    ==========================  ====================================
    Scheme                      Store
    ==========================  ====================================
    ``memory://``               :class:`MemoryStore`
    ``sqlite://runs.db``        :class:`SQLiteStore`, relative to the cwd
    ``sqlite:///var/lib/x.db``  :class:`SQLiteStore`, absolute
    ``postgres://…``            :class:`PostgresStore` (extra: postgres)
    ``mongodb://…``             :class:`MongoStore` (extra: mongo)
    ==========================  ====================================

    **Two slashes for a relative path, three for an absolute one.** A URL's
    authority position is what holds a bare name, so ``sqlite://runs.db`` is
    ``runs.db`` beside you and ``sqlite:///runs.db`` is ``/runs.db`` at the
    filesystem root -- which is a real path, just never the intended one. That
    differs from SQLAlchemy, where three slashes is the relative form; it is
    fixed here rather than translated because ``f"sqlite://{path}"`` over an
    absolute path is how every caller in this repo spells the absolute case,
    and reinterpreting three slashes would silently redirect those writes to a
    relative file rather than failing. :class:`SQLiteStore` says all of this
    again if the path it is handed cannot be opened.

    Raises :class:`ConfigurationError` for an unknown scheme, or for a known one
    whose driver is not installed — with the ``pip install`` line that fixes it,
    since that is the actual next step.
    """

    parsed = urlparse(url)
    scheme = parsed.scheme or "memory"

    if scheme == "memory":
        from loom.stores.memory import MemoryStore

        return MemoryStore()

    if scheme == "sqlite":
        from loom.stores.sqlite import SQLiteStore

        # sqlite://relative.db keeps the name in netloc; sqlite:///abs.db in path.
        path = f"{parsed.netloc}{parsed.path}" or ":memory:"
        return SQLiteStore(path)

    if scheme in ("postgres", "postgresql"):
        try:
            from loom.stores.postgres import PostgresStore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ConfigurationError(
                "PostgresStore needs asyncpg: pip install 'loomsdk[postgres]'"
            ) from exc
        # asyncpg wants the postgresql:// spelling.
        return PostgresStore(url.replace("postgres://", "postgresql://", 1))

    if scheme in ("mongodb", "mongodb+srv"):
        try:
            from loom.stores.mongo import MongoStore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ConfigurationError(
                "MongoStore needs motor: pip install 'loomsdk[mongo]'"
            ) from exc
        database = parsed.path.lstrip("/") or "loom"
        return MongoStore(url, database)

    raise ConfigurationError(
        f"unsupported store URL {url!r}. Use memory://, sqlite://runs.db, "
        "postgres://…, or mongodb://… — or construct the store yourself and pass "
        "store= to Runtime()."
    )


def store_from_env(default: str = DEFAULT_STORE_URL) -> Any:
    """Build the store named by ``$LOOM_STORE``, falling back to *default*."""
    return from_url(os.environ.get(STORE_URL_ENV, default))
