"""Resource declaration and dependency injection.

A ``@resource`` decorated async factory function produces a resource
(DB connection, HTTP client, etc.).  Steps declare dependencies via
``Depends(my_resource)`` and receive the resolved instance at call time.

Scoping controls lifecycle:

- ``FLOW``: One instance per workflow invocation (default).
- ``WORKER``: Shared across runs on the same worker process.
- ``GLOBAL``: Shared across all workers (singleton).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar, overload

T = TypeVar("T")


class ResourceScope(StrEnum):
    """Lifecycle scope for a resource instance."""

    FLOW = "flow"
    WORKER = "worker"
    GLOBAL = "global"


@dataclass
class ResourceDefinition:
    """A declared resource — wraps the factory function with metadata."""

    factory: Callable[..., Coroutine[Any, Any, Any]]
    scope: ResourceScope = ResourceScope.FLOW
    name: str = ""
    health_check: Callable[..., Coroutine[Any, Any, bool]] | None = None
    _instances: dict[str, Any] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def acquire(self, scope_key: str = "default") -> Any:
        """Get or create the resource instance for a given scope key."""
        if scope_key in self._instances:
            return self._instances[scope_key]
        async with self._lock:
            if scope_key in self._instances:
                return self._instances[scope_key]
            instance = await self.factory()
            self._instances[scope_key] = instance
            return instance

    async def release(self, scope_key: str = "default") -> None:
        """Release a resource instance."""
        instance = self._instances.pop(scope_key, None)
        if instance is None:
            return
        # Call close/aclose/cleanup if available
        for method_name in ("aclose", "close", "cleanup", "disconnect"):
            method = getattr(instance, method_name, None)
            if callable(method):
                result = method()
                if asyncio.iscoroutine(result):
                    await result
                break

    async def release_all(self) -> None:
        """Release all instances."""
        keys = list(self._instances.keys())
        for key in keys:
            await self.release(key)

    async def is_healthy(self) -> bool:
        """Run the health check if one is configured."""
        if self.health_check is None:
            return True
        return await self.health_check()


@overload
def resource(fn: Callable[..., Coroutine[Any, Any, T]]) -> ResourceDefinition: ...


@overload
def resource(
    *,
    scope: ResourceScope = ResourceScope.FLOW,
    name: str = "",
    health: Callable[..., Coroutine[Any, Any, bool]] | None = None,
) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], ResourceDefinition]: ...


def resource(
    fn: Callable[..., Coroutine[Any, Any, Any]] | None = None,
    *,
    scope: ResourceScope = ResourceScope.FLOW,
    name: str = "",
    health: Callable[..., Coroutine[Any, Any, bool]] | None = None,
) -> ResourceDefinition | Callable[..., ResourceDefinition]:
    """Declare an external resource (DB, cache, HTTP client).

    Can be used as a bare decorator or with arguments::

        @resource
        async def db_pool():
            return await create_pool(...)

        @resource(scope=ResourceScope.WORKER)
        async def http_client():
            return httpx.AsyncClient()
    """

    def decorator(
        f: Callable[..., Coroutine[Any, Any, Any]],
    ) -> ResourceDefinition:
        return ResourceDefinition(
            factory=f,
            scope=scope,
            name=name or f.__name__,
            health_check=health,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


class Depends:
    """Declare a dependency on a resource within a step.

    Usage::

        @step
        async def query_db(ctx: StepContext, sql: str, db=Depends(db_pool)):
            return await db.fetch(sql)
    """

    def __init__(self, resource_def: ResourceDefinition) -> None:
        self._def = resource_def

    @property
    def resource(self) -> ResourceDefinition:
        return self._def

    async def resolve(self, scope_key: str = "default") -> Any:
        """Resolve the dependency to an instance."""
        return await self._def.acquire(scope_key)

    def __repr__(self) -> str:
        return f"Depends({self._def.name!r})"
