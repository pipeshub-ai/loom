"""Durable state that outlives a run, and output that streams while one lasts.

Two ports that answer two questions a journal deliberately cannot.

**What did the last run leave behind?** A journal is per run, and a workflow
that polls a feed needs to remember the last cursor it saw. `StateStore` is a
key-value space scoped to `(workflow, key)` — mutable and current, where an
artifact is immutable and versioned. The alternative is a global somewhere
outside the runtime, which does not survive a restart.

**What is it doing right now?** A run that takes four minutes has nothing to say
for four minutes. `RunStream` is where `ctx.report` goes, so an operator, a CLI
watching a run, or an assistant driving one can see progress rather than a
spinner.

Both ship a reference adapter that needs no infrastructure — the execution store
for state, memory for the stream — and both are ports because the interesting
implementations belong to whoever is hosting: Redis, a websocket fan-out, a log
pipeline.

>>> import asyncio
>>> from workflow_builder.runtime.state import InMemoryRunStream, Report
>>> stream = InMemoryRunStream()
>>> asyncio.run(stream.report("run_1", "fetching page 1"))
>>> [entry.message for entry in stream.since("run_1")]
['fetching page 1']
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "InMemoryRunStream",
    "Report",
    "RunStream",
    "StateStore",
    "StoreBackedState",
]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@runtime_checkable
class StateStore(Protocol):
    """Workflow-scoped key-value state that survives across runs.

    Keyed by ``(workflow, key)``: state belongs to a workflow, not to any one
    run of it.
    """

    async def get(self, workflow: str, key: str) -> Any | None:
        """The stored value, or ``None``."""
        ...

    async def set(self, workflow: str, key: str, value: Any) -> None:
        """Store *value*, replacing whatever was there."""
        ...

    async def delete(self, workflow: str, key: str) -> None: ...

    async def keys(self, workflow: str) -> list[str]:
        """Every key this workflow holds, sorted."""
        ...


class StoreBackedState:
    """The reference adapter: workflow state in the execution store's cache.

    Chosen so that ``ctx.state`` needs no new infrastructure — whatever backs
    the journal backs this, which for a laptop is one SQLite file and for
    production is the database already in the deployment.

    Values must survive :func:`encode`/:func:`decode`, which is the same
    constraint the journal imposes. A workflow whose state cannot be serialised
    is a workflow whose state cannot be durable, and saying so at the write is
    better than discovering it at the read.
    """

    #: Namespaced so workflow state cannot collide with a step's cache entry.
    PREFIX = "state"

    def __init__(self, store: Any) -> None:
        self._store = store

    @classmethod
    def _key(cls, workflow: str, key: str) -> str:
        return f"{cls.PREFIX}:{workflow}:{key}"

    async def get(self, workflow: str, key: str) -> Any | None:
        from workflow_builder.core.serde import decode

        found = await self._store.get(self._key(workflow, key))
        return None if found is None else decode(found)

    async def set(self, workflow: str, key: str, value: Any) -> None:
        from workflow_builder.core.serde import encode

        # No TTL: this is state, not a cache. A value that expired on its own
        # would make a workflow's memory depend on how long it went unused.
        await self._store.set(self._key(workflow, key), encode(value), 0)
        await self._index(workflow, key, present=True)

    async def delete(self, workflow: str, key: str) -> None:
        await self._store.delete(self._key(workflow, key))
        await self._index(workflow, key, present=False)

    async def keys(self, workflow: str) -> list[str]:
        index = await self._store.get(self._key(workflow, "__keys__"))
        return sorted(index or [])

    async def _index(self, workflow: str, key: str, *, present: bool) -> None:
        """Maintain the key list.

        A ``CacheStore`` can look a key up but cannot enumerate one, so listing
        needs an index kept alongside. Held as one value rather than a scan,
        because the alternative — asking the store for everything matching a
        prefix — is not something every backend can do.
        """
        index_key = self._key(workflow, "__keys__")
        current = set(await self._store.get(index_key) or [])
        updated = current | {key} if present else current - {key}
        if updated != current:
            await self._store.set(index_key, sorted(updated), 0)


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    """One thing a run said about itself."""

    run_id: str
    message: str
    kind: str = "text"
    """``text`` by default. A host may use its own kinds — ``progress``,
    ``partial``, ``thinking`` — and LOOM passes them through without meaning."""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def describe(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "message": self.message,
            "kind": self.kind,
            "at": self.at.isoformat(),
        }


@runtime_checkable
class RunStream(Protocol):
    """Where a run's progress goes."""

    async def report(self, run_id: str, message: str, *, kind: str = "text") -> None:
        """Record something the run wants observed while it is still running."""
        ...


class InMemoryRunStream:
    """The reference adapter: a bounded ring per run.

    Bounded on purpose. A stream is a convenience for watching, not a second
    journal, and an unbounded one turns a chatty long-running workflow into a
    memory leak that only shows up in the deployments that need it most.

    In-process only, which is the honest limit: a run reported on by one worker
    is not visible to another. A host that needs that ships an adapter over
    whatever it already uses to fan out — that is what the port is for.
    """

    def __init__(self, *, per_run: int = 500) -> None:
        self._reports: dict[str, deque[Report]] = defaultdict(
            lambda: deque(maxlen=per_run)
        )
        self._waiters: dict[str, list[asyncio.Event]] = defaultdict(list)

    async def report(self, run_id: str, message: str, *, kind: str = "text") -> None:
        self._reports[run_id].append(Report(run_id=run_id, message=message, kind=kind))
        for waiter in self._waiters.pop(run_id, []):
            waiter.set()

    def since(self, run_id: str, offset: int = 0) -> list[Report]:
        """Reports from *offset* onward, for a caller that is polling."""
        return list(self._reports.get(run_id, ()))[offset:]

    async def wait(self, run_id: str, *, timeout: float = 1.0) -> None:
        """Block until the run says something, or *timeout* passes.

        Lets a watcher follow a run without spinning on `since`.
        """
        event = asyncio.Event()
        self._waiters[run_id].append(event)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            with_ = self._waiters.get(run_id)
            if with_ and event in with_:
                with_.remove(event)

    def forget(self, run_id: str) -> None:
        """Drop a finished run's reports."""
        self._reports.pop(run_id, None)
        self._waiters.pop(run_id, None)
