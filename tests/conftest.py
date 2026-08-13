"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore


@pytest.fixture(autouse=True)
def isolated_catalog() -> Iterator[object]:
    """Snapshot and restore the process-global toolset catalog.

    ``register_toolset`` writes to a module-level singleton, so without this a
    test that registers a manifest changes what every later test sees — the
    kind of ordering dependency that only shows up in a full-suite run.
    """
    from workflow_builder.toolsets.registry import get_catalog

    catalog = get_catalog()
    saved = dict(catalog._manifests)
    try:
        yield catalog
    finally:
        catalog._manifests.clear()
        catalog._manifests.update(saved)


@pytest.fixture
def memory_store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def runtime(memory_store: MemoryStore) -> Runtime:
    return Runtime(store=memory_store)


@step
async def add_one(x: int) -> int:
    return x + 1


@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"


@step
async def failing_step(x: int) -> int:
    raise ValueError(f"intentional failure on {x}")


@workflow
async def identity_workflow(ctx: Context, data: str) -> str:
    return data


@workflow
async def greet_workflow(ctx: Context, name: str) -> str:
    return await ctx.step(greet, name)
