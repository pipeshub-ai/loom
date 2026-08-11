"""Leader election for distributed scheduler coordination.

Uses the ``LockProvider`` protocol from ``state.base`` to elect a single
leader per group. Only the leader runs the scheduler tick, timer wheel,
and cron triggers.
"""

from __future__ import annotations

import time

from workflow_builder.state.base import LockProvider


class LeaderElector:
    """Coordinates leadership across nodes using a :class:`LockProvider`.

    Each node identifies itself with a unique *node_id*.  Leadership is
    scoped to a *group* string — typically the cluster or namespace name —
    so multiple independent schedulers can coexist.
    """

    def __init__(self, lock_provider: LockProvider, node_id: str) -> None:
        self._lock_provider = lock_provider
        self._node_id = node_id

    @property
    def node_id(self) -> str:
        return self._node_id

    async def acquire_leadership(self, group: str, ttl: float = 30.0) -> bool:
        """Attempt to become the leader for *group*.

        Returns ``True`` if this node acquired (or already held) the lock.
        """
        return await self._lock_provider.acquire(group, self._node_id, ttl)

    async def renew(self, group: str, ttl: float = 30.0) -> bool:
        """Extend the leadership lease.

        Returns ``True`` only if this node still holds the lock.
        """
        return await self._lock_provider.renew(group, self._node_id, ttl)

    async def release(self, group: str) -> None:
        """Voluntarily give up leadership for *group*."""
        await self._lock_provider.release(group, self._node_id)

    async def is_leader(self, group: str) -> bool:
        """Check whether this node is the current leader.

        Performs a renewal attempt and returns the result.
        """
        return await self.renew(group)


class InMemoryLockProvider:
    """In-process :class:`LockProvider` backed by a plain dict.

    Useful for tests and single-process deployments where distributed
    locking is unnecessary.  Expiry is based on :func:`time.monotonic`.
    """

    def __init__(self) -> None:
        self._locks: dict[str, tuple[str, float]] = {}

    async def acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        now = time.monotonic()
        entry = self._locks.get(key)
        if entry is not None:
            existing_owner, expires_at = entry
            if existing_owner != owner and expires_at > now:
                return False
        self._locks[key] = (owner, now + ttl_seconds)
        return True

    async def renew(self, key: str, owner: str, ttl_seconds: float) -> bool:
        now = time.monotonic()
        entry = self._locks.get(key)
        if entry is None:
            return False
        existing_owner, expires_at = entry
        if existing_owner != owner or expires_at <= now:
            return False
        self._locks[key] = (owner, now + ttl_seconds)
        return True

    async def release(self, key: str, owner: str) -> None:
        entry = self._locks.get(key)
        if entry is not None and entry[0] == owner:
            del self._locks[key]
