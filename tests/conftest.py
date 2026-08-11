"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore


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
