"""Tests for Page[T], grant derivation, and resource system."""

from __future__ import annotations

import pytest

from workflow_builder.core.types import Page
from workflow_builder.resources.base import (
    Depends,
    ResourceDefinition,
    ResourceScope,
    resource,
)
from workflow_builder.security.grants import GrantSet, derive_grants

# ---------------------------------------------------------------------------
# Page[T]
# ---------------------------------------------------------------------------


class TestPage:
    def test_basic_page(self) -> None:
        page = Page(items=[1, 2, 3], has_more=True, cursor="abc")
        assert page.items == [1, 2, 3]
        assert page.has_more is True
        assert page.cursor == "abc"
        assert page.total is None
        assert len(page) == 3

    def test_page_iteration(self) -> None:
        page = Page(items=["a", "b", "c"])
        assert list(page) == ["a", "b", "c"]

    def test_page_no_more(self) -> None:
        page = Page(items=[1], has_more=False)
        assert page.has_more is False
        assert page.cursor is None

    def test_page_with_total(self) -> None:
        page = Page(items=[1, 2], total=100, has_more=True, cursor="next")
        assert page.total == 100

    def test_collect_single(self) -> None:
        page = Page(items=[1, 2, 3])
        assert page.collect() == [1, 2, 3]

    def test_collect_multiple_pages(self) -> None:
        p1 = Page(items=[1, 2], has_more=True, cursor="a")
        p2 = Page(items=[3, 4], has_more=True, cursor="b")
        p3 = Page(items=[5])
        result = p1.collect(p2, p3)
        assert result == [1, 2, 3, 4, 5]

    def test_collect_max_items(self) -> None:
        p1 = Page(items=[1, 2, 3])
        p2 = Page(items=[4, 5, 6])
        result = p1.collect(p2, max_items=4)
        assert result == [1, 2, 3, 4]

    def test_repr(self) -> None:
        page = Page(items=[1, 2], has_more=True, total=10)
        r = repr(page)
        assert "items=2" in r
        assert "has_more" in r
        assert "total=10" in r

    def test_repr_no_more(self) -> None:
        page = Page(items=[1])
        r = repr(page)
        assert "has_more" not in r

    def test_empty_page(self) -> None:
        page: Page[str] = Page(items=[])
        assert len(page) == 0
        assert list(page) == []
        assert page.has_more is False


# ---------------------------------------------------------------------------
# GrantSet
# ---------------------------------------------------------------------------


class TestGrantSet:
    def test_empty_grant_set(self) -> None:
        gs = GrantSet()
        assert gs.is_empty is True

    def test_non_empty_grant_set(self) -> None:
        gs = GrantSet(toolsets=["jira.issues:write"])
        assert gs.is_empty is False

    def test_merge(self) -> None:
        gs1 = GrantSet(toolsets=["jira"], agents=["triage"])
        gs2 = GrantSet(toolsets=["slack"], egress=["slack.com"])
        merged = gs1.merge(gs2)
        assert set(merged.toolsets) == {"jira", "slack"}
        assert merged.agents == ["triage"]
        assert merged.egress == ["slack.com"]

    def test_merge_deduplication(self) -> None:
        gs1 = GrantSet(toolsets=["jira", "slack"])
        gs2 = GrantSet(toolsets=["slack", "github"])
        merged = gs1.merge(gs2)
        assert merged.toolsets == ["jira", "slack", "github"]


# ---------------------------------------------------------------------------
# derive_grants — AST analysis
# ---------------------------------------------------------------------------


class TestDeriveGrants:
    def test_agent_calls(self) -> None:
        source = '''
async def my_workflow(ctx):
    result = await ctx.agent("triage-agent", ticket)
    result2 = await ctx.agent("review-agent", data)
'''
        grants = derive_grants(source)
        assert "triage-agent" in grants.agents
        assert "review-agent" in grants.agents

    def test_child_calls(self) -> None:
        source = '''
async def my_workflow(ctx):
    result = await ctx.child("sub-workflow", input_data)
'''
        grants = derive_grants(source)
        assert "sub-workflow" in grants.subflows

    def test_toolset_imports(self) -> None:
        source = '''
from loom_toolset_jira import JiraClient
import loom_toolset_slack
'''
        grants = derive_grants(source)
        assert "jira" in grants.toolsets
        assert "slack" in grants.toolsets

    def test_no_grants(self) -> None:
        source = '''
def simple_function():
    return 42
'''
        grants = derive_grants(source)
        assert grants.is_empty is True

    def test_syntax_error_returns_empty(self) -> None:
        grants = derive_grants("this is not valid python {{{}}")
        assert grants.is_empty is True

    def test_variable_agent_name(self) -> None:
        source = '''
async def my_workflow(ctx):
    result = await ctx.agent(agent_var, data)
'''
        grants = derive_grants(source)
        assert "agent_var" in grants.agents


# ---------------------------------------------------------------------------
# Resource system
# ---------------------------------------------------------------------------


class TestResourceDecorator:
    def test_bare_decorator(self) -> None:
        @resource
        async def db_pool():
            return "pool"

        assert isinstance(db_pool, ResourceDefinition)
        assert db_pool.name == "db_pool"
        assert db_pool.scope is ResourceScope.FLOW

    def test_decorator_with_args(self) -> None:
        @resource(scope=ResourceScope.WORKER, name="my_pool")
        async def db_pool():
            return "pool"

        assert isinstance(db_pool, ResourceDefinition)
        assert db_pool.name == "my_pool"
        assert db_pool.scope is ResourceScope.WORKER

    @pytest.mark.asyncio
    async def test_acquire(self) -> None:
        @resource
        async def counter():
            return {"count": 0}

        instance = await counter.acquire()
        assert instance == {"count": 0}

    @pytest.mark.asyncio
    async def test_acquire_same_scope_returns_same(self) -> None:
        call_count = 0

        @resource
        async def tracked():
            nonlocal call_count
            call_count += 1
            return f"instance-{call_count}"

        first = await tracked.acquire("key1")
        second = await tracked.acquire("key1")
        assert first is second
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_acquire_different_scope(self) -> None:
        call_count = 0

        @resource
        async def tracked():
            nonlocal call_count
            call_count += 1
            return f"instance-{call_count}"

        first = await tracked.acquire("key1")
        second = await tracked.acquire("key2")
        assert first != second
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_release(self) -> None:
        closed = False

        class FakePool:
            async def close(self):
                nonlocal closed
                closed = True

        @resource
        async def pool():
            return FakePool()

        await pool.acquire("k")
        await pool.release("k")
        assert closed is True

    @pytest.mark.asyncio
    async def test_release_all(self) -> None:
        instances = []

        class FakeConn:
            async def close(self):
                instances.append("closed")

        @resource
        async def conn():
            return FakeConn()

        await conn.acquire("a")
        await conn.acquire("b")
        await conn.release_all()
        assert len(instances) == 2

    @pytest.mark.asyncio
    async def test_health_check(self) -> None:
        async def check():
            return True

        @resource(health=check)
        async def pool():
            return "pool"

        assert await pool.is_healthy() is True

    @pytest.mark.asyncio
    async def test_health_check_not_configured(self) -> None:
        @resource
        async def pool():
            return "pool"

        assert await pool.is_healthy() is True


class TestDepends:
    def test_repr(self) -> None:
        @resource(name="my_db")
        async def db():
            return "conn"

        dep = Depends(db)
        assert "my_db" in repr(dep)

    @pytest.mark.asyncio
    async def test_resolve(self) -> None:
        @resource
        async def db():
            return "connection"

        dep = Depends(db)
        result = await dep.resolve()
        assert result == "connection"

    def test_resource_property(self) -> None:
        @resource
        async def db():
            return "conn"

        dep = Depends(db)
        assert dep.resource is db
