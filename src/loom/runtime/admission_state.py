"""Where flow-control counters live.

``AdmissionController`` held every counter in a process-local ``dict``, and
said so in its own docstring — while ``CLAUDE.md`` listed flow control under
"Production Layer". Both cannot be right. In-memory counters mean
``Runtime(admission=...)`` provides no concurrency limit, no rate limit and no
singleton guarantee in any multi-worker deployment, which is the only kind that
has these problems: two workers each admit up to the limit, so a
``concurrency=1`` policy runs two.

So the state becomes a port. :class:`InMemoryAdmissionState` is the reference —
identical behaviour to what shipped, and correct for a single process — and
:class:`StoreBackedAdmissionState` is built on ``CacheStore`` + ``LockProvider``,
which every LOOM store already implements. That is the same shape
:class:`~loom.events.log.StoreBackedEventLog` takes, and for the same reason:
it lands with no new store methods and no migration.

**Idle keys expire.** The in-memory dicts grew one entry per partition key and
never shrank, so a policy partitioned by customer id was a memory leak the size
of the customer list. Every entry here carries a TTL.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AdmissionState",
    "InMemoryAdmissionState",
    "StoreBackedAdmissionState",
]

#: How long an untouched key survives. Long enough that a slow schedule keeps
#: its window, short enough that a high-cardinality partition key does not
#: accumulate for the life of the process.
DEFAULT_TTL_SECONDS = 3600.0


@runtime_checkable
class AdmissionState(Protocol):
    """The counters and timestamps an admission policy decides against.

    Deliberately five small methods rather than one per policy. A policy is a
    rule over *numbers*; where those numbers live is a deployment decision, and
    a port shaped like the rules would have to change every time a rule does.
    """

    async def in_flight(self, key: str) -> int:
        """How many runs of *key* are live."""
        ...

    async def enter(self, key: str) -> None:
        """Record that a run of *key* has started."""
        ...

    async def leave(self, key: str) -> None:
        """Record that a run of *key* has finished."""
        ...

    async def read(self, key: str, default: Any = None) -> Any:
        """A stored value — a timestamp, a window, a batch count."""
        ...

    async def write(self, key: str, value: Any) -> None:
        """Store a value, with the implementation's expiry."""
        ...


@dataclass
class InMemoryAdmissionState:
    """Process-local state. The reference implementation, and the default.

    Correct for a single process and honest about being nothing more. Every
    entry carries a deadline so a high-cardinality partition key does not leak.
    """

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    _counts: dict[str, int] = field(default_factory=dict)
    _values: dict[str, tuple[float, Any]] = field(default_factory=dict)

    async def in_flight(self, key: str) -> int:
        return self._counts.get(key, 0)

    async def enter(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    async def leave(self, key: str) -> None:
        remaining = self._counts.get(key, 0) - 1
        if remaining > 0:
            self._counts[key] = remaining
        else:
            # Removed rather than left at zero: a key nobody is running is a key
            # nobody needs an entry for, and "counts down to zero and stays"
            # is how the leak looked.
            self._counts.pop(key, None)

    async def read(self, key: str, default: Any = None) -> Any:
        self._expire()
        entry = self._values.get(key)
        return default if entry is None else entry[1]

    async def write(self, key: str, value: Any) -> None:
        self._values[key] = (time.monotonic() + self.ttl_seconds, value)

    def _expire(self) -> None:
        """Drop entries nothing has touched within the TTL.

        On read rather than on a timer: a controller with no traffic should not
        keep a task alive, and a controller with traffic sweeps as a side effect
        of being used.
        """
        now = time.monotonic()
        stale = [key for key, (deadline, _) in self._values.items() if deadline <= now]
        for key in stale:
            del self._values[key]

    def __repr__(self) -> str:
        return (
            f"<InMemoryAdmissionState in_flight={len(self._counts)} "
            f"windows={len(self._values)}>"
        )


class StoreBackedAdmissionState:
    """Shared state over any LOOM store. What a multi-process deployment needs.

    Uses only ``CacheStore`` (``get``/``set``/``delete``) and ``LockProvider``,
    both of which memory, SQLite, Postgres and Mongo already implement.

    The in-flight counter is read-modify-written under the store's own lock,
    because that is the operation two workers race on: without it, both read
    the same count, both find room, and a ``concurrency=1`` policy admits two.
    """

    def __init__(
        self,
        store: Any,
        *,
        owner: str = "",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        namespace: str = "admission",
    ) -> None:
        import secrets

        from loom.core.exceptions import ConfigurationError

        for capability in ("get", "set", "delete"):
            if not hasattr(store, capability):
                raise ConfigurationError(
                    f"StoreBackedAdmissionState needs a CacheStore; "
                    f"{type(store).__name__} has no '{capability}'."
                )
        self._store = store
        self._lock = store if hasattr(store, "acquire") else None
        self._owner = owner or f"admission-{secrets.token_hex(4)}"
        self._ttl = ttl_seconds
        self._namespace = namespace

    def _key(self, key: str, kind: str) -> str:
        return f"{self._namespace}:{kind}:{key}"

    async def in_flight(self, key: str) -> int:
        return int(await self._store.get(self._key(key, "count")) or 0)

    async def enter(self, key: str) -> None:
        await self._adjust(key, +1)

    async def leave(self, key: str) -> None:
        await self._adjust(key, -1)

    async def _adjust(self, key: str, delta: int) -> None:
        """Move the counter under the store's lock.

        The lock is what makes this a limit rather than a suggestion. Without
        it the read and the write are two round trips with somebody else's in
        between — which is exactly the window a second worker admits through.
        """
        name = self._key(key, "count")
        async with _Guarded(self._lock, f"{name}:lock", self._owner):
            current = int(await self._store.get(name) or 0)
            updated = max(0, current + delta)
            if updated:
                # In-flight counters expire too. A worker that dies holding one
                # would otherwise hold a concurrency slot forever, and the
                # symptom — every later run delayed, none running — is the
                # hardest kind of problem to attribute.
                await self._store.set(name, updated, self._ttl)
            else:
                await self._store.delete(name)

    async def read(self, key: str, default: Any = None) -> Any:
        found = await self._store.get(self._key(key, "value"))
        return default if found is None else found

    async def write(self, key: str, value: Any) -> None:
        await self._store.set(self._key(key, "value"), value, self._ttl)

    def __repr__(self) -> str:
        return f"<StoreBackedAdmissionState {type(self._store).__name__}>"


class _Guarded:
    """The store's lock, or nothing when it does not provide one.

    A store without ``LockProvider`` still works — it simply gives the
    single-process guarantee, which is what it had before this existed. Failing
    instead would make an optional capability mandatory.
    """

    def __init__(self, lock: Any, key: str, owner: str) -> None:
        self._lock = lock
        self._key = key
        self._owner = owner
        self._held = False

    async def __aenter__(self) -> None:
        if self._lock is None:
            return
        self._held = await self._lock.acquire(self._key, self._owner, 10.0)

    async def __aexit__(self, *_: Any) -> None:
        if self._held and self._lock is not None:
            await self._lock.release(self._key, self._owner)
