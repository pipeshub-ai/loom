"""Redis-backed cache and locks.

Deliberately **not** an `ExecutionStore`. A journal wants durability, ordered
scans, and queries by workflow and status; Redis is the wrong shape for all
three, and offering it would invite a deployment that loses runs on an eviction.
What Redis is genuinely the best answer for is the other two protocols — a
cache shared across processes, and a lease that several workers contend for.

So the deployment this enables is: one durable store for the journal, Redis
beside it for coordination.

    store = PostgresStore(dsn)
    coordination = RedisStore(url="redis://localhost:6379/0")
    rt = Runtime(store=store, cache=coordination)
    await rt.start_scheduler(elector=LeaderElector(coordination, node_id))

Requires ``redis``::

    pip install loomflow[redis]
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["RedisStore"]

#: Prefixes, so a Redis shared with an application does not collide with it and
#: an operator can see at a glance which keys are LOOM's.
CACHE_PREFIX = "loom:cache:"
LOCK_PREFIX = "loom:lock:"


class RedisStore:
    """Implements :class:`CacheStore` and :class:`LockProvider`.

    Pass a ``url`` and a client is created on first use, or pass a ``client``
    directly — which is what the tests do, and what a host with its own pool
    should do rather than opening a second one.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        client: Any | None = None,
    ) -> None:
        self._url = url
        self._client = client

    def _redis(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis  # imported late: an optional extra

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None) or getattr(
                self._client, "close", None
            )
            if close is not None:
                await close()
            self._client = None

    # -- CacheStore ---------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        found = await self._redis().get(CACHE_PREFIX + key)
        if found is None:
            return None
        try:
            return json.loads(found)
        except (TypeError, ValueError):
            # Written by something other than this store. Hand it back rather
            # than raising: a cache is an optimisation, not a source of truth.
            return found

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store ``value``. A ``ttl_seconds`` of zero or less means no expiry.

        The same rule the other stores follow — reading it as "expires
        immediately" would make ``set(key, value, 0)`` a silent no-op, which is
        never what a caller means.
        """
        payload = json.dumps(value)
        if ttl_seconds > 0:
            await self._redis().set(CACHE_PREFIX + key, payload, ex=int(ttl_seconds))
        else:
            await self._redis().set(CACHE_PREFIX + key, payload)

    async def delete(self, key: str) -> None:
        await self._redis().delete(CACHE_PREFIX + key)

    # -- LockProvider -------------------------------------------------------

    async def acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        """Take the lock if nobody holds it. Re-entrant for the same owner.

        ``SET NX PX`` in one round trip: check-then-set would let two workers
        both observe a free lock and both take it, which is the one thing a
        lease exists to prevent.
        """
        redis = self._redis()
        name = LOCK_PREFIX + key
        expiry = max(1, int(ttl_seconds * 1000))
        if await redis.set(name, owner, nx=True, px=expiry):
            return True
        # Already ours: extend rather than fail, so a heartbeat is one call.
        return await redis.get(name) == owner and bool(
            await redis.set(name, owner, px=expiry)
        )

    async def renew(self, key: str, owner: str, ttl_seconds: float) -> bool:
        """Extend a lock this owner holds. ``False`` if it was lost."""
        redis = self._redis()
        name = LOCK_PREFIX + key
        if await redis.get(name) != owner:
            return False
        await redis.set(name, owner, px=max(1, int(ttl_seconds * 1000)))
        return True

    async def release(self, key: str, owner: str) -> None:
        """Release only if still held by *owner*.

        An unconditional delete would let a worker whose lease already expired
        release the lock a *different* worker has since taken.
        """
        redis = self._redis()
        name = LOCK_PREFIX + key
        if await redis.get(name) == owner:
            await redis.delete(name)

    def __repr__(self) -> str:
        return f"<RedisStore {self._url}>"
