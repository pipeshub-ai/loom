"""Tests for the three-layer lazy tool system."""

from __future__ import annotations

import pytest


class TestToolset:
    def test_from_steps(self) -> None:
        from loom import Retry, step
        from loom.agents.tool_registry import Toolset

        @step(retry=Retry(max_attempts=2))
        async def my_search(query: str) -> list:
            """Search for items.

            Args:
                query: Search query string.
            """
            return []

        @step
        async def my_create(name: str, value: int = 0) -> dict:
            """Create an item."""
            return {"name": name, "value": value}

        ts = Toolset.from_steps("test", [my_search, my_create])
        assert ts.manifest.id == "test"
        assert len(ts.manifest.all_operations()) == 2

        # Resolve a tool
        tool = ts.resolve("my_search")
        assert tool.name == "my_search"
        assert "query" in tool.parameters.get("properties", {})

    def test_from_callables(self) -> None:
        from loom.agents.tool_registry import Toolset

        async def web_search(query: str) -> str:
            """Search the web."""
            return f"results for {query}"

        ts = Toolset.from_callables("web", [web_search], summary="Web tools")
        assert ts.manifest.id == "web"
        assert ts.manifest.summary == "Web tools"
        assert len(ts.manifest.all_operations()) == 1

    def test_resolve_unknown_raises(self) -> None:
        from loom.agents.tool_registry import Toolset

        async def noop() -> None:
            pass

        ts = Toolset.from_callables("x", [noop])
        with pytest.raises(KeyError):
            ts.resolve("nonexistent")

    def test_resolve_all(self) -> None:
        from loom import step
        from loom.agents.tool_registry import Toolset

        @step
        async def a() -> str:
            """Tool A."""
            return "a"

        @step
        async def b() -> str:
            """Tool B."""
            return "b"

        ts = Toolset.from_steps("ab", [a, b])
        tools = ts.resolve_all()
        assert len(tools) == 2


class TestToolsetRegistry:
    def test_register_and_list(self) -> None:
        from loom.agents.tool_registry import (
            Toolset,
            ToolsetRegistry,
        )

        registry = ToolsetRegistry()

        async def fn() -> str:
            """A tool."""
            return ""

        registry.register(Toolset.from_callables("alpha", [fn]))
        registry.register(Toolset.from_callables("beta", [fn]))

        assert sorted(registry.list_toolsets()) == ["alpha", "beta"]

    def test_resolve_tools_all(self) -> None:
        from loom import step
        from loom.agents.tool_registry import (
            Toolset,
            ToolsetRegistry,
        )

        @step
        async def s1() -> str:
            """S1."""
            return ""

        @step
        async def s2() -> str:
            """S2."""
            return ""

        registry = ToolsetRegistry()
        registry.register(Toolset.from_steps("a", [s1]))
        registry.register(Toolset.from_steps("b", [s2]))

        tools = registry.resolve_tools()
        assert len(tools) == 2

    def test_resolve_tools_selective(self) -> None:
        from loom import step
        from loom.agents.tool_registry import (
            Toolset,
            ToolsetRegistry,
        )

        @step
        async def x() -> str:
            """X."""
            return ""

        @step
        async def y() -> str:
            """Y."""
            return ""

        registry = ToolsetRegistry()
        registry.register(Toolset.from_steps("one", [x]))
        registry.register(Toolset.from_steps("two", [y]))

        tools = registry.resolve_tools(["one"])
        assert len(tools) == 1

    def test_describe_auto_generated(self) -> None:
        from loom import step
        from loom.agents.tool_registry import (
            Toolset,
            ToolsetRegistry,
        )

        @step
        async def jira_search(jql: str, limit: int = 10) -> list:
            """Search Jira issues using JQL."""
            return []

        registry = ToolsetRegistry()
        registry.register(Toolset.from_steps("jira", [jira_search]))

        desc = registry.describe()
        assert "jira" in desc
        assert "jira_search" in desc
        assert "jql" in desc
        assert "limit" in desc
        # No hand-written docs needed
        assert "Available tools" in desc

    def test_describe_empty(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry

        assert ToolsetRegistry().describe() == ""

    def test_resolve_one(self) -> None:
        from loom import step
        from loom.agents.tool_registry import (
            Toolset,
            ToolsetRegistry,
        )

        @step
        async def my_fn() -> str:
            """Do stuff."""
            return ""

        registry = ToolsetRegistry()
        registry.register(Toolset.from_steps("ts", [my_fn]))

        tool = registry.resolve_one("ts", "my_fn")
        assert tool.name == "my_fn"

    def test_resolve_one_unknown_toolset(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.core.exceptions import RegistryError

        with pytest.raises(RegistryError):
            ToolsetRegistry().resolve_one("nope", "op")

    def test_resolve_tools_rejects_unknown_id(self) -> None:
        """A typo'd toolset id must not silently yield fewer tools."""
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.core.exceptions import RegistryError

        with pytest.raises(RegistryError, match="jria"):
            ToolsetRegistry().resolve_tools(["jria"])


class TestRuntimeToolsetIntegration:
    def test_runtime_has_toolsets(self) -> None:
        from loom import Runtime

        rt = Runtime()
        assert hasattr(rt, "toolsets")
        assert rt.toolsets.list_toolsets() == []

    @pytest.mark.asyncio
    async def test_ctx_agent_resolves_tools(self) -> None:
        from loom import Context, Runtime, workflow
        from loom.agents.result import AgentResult
        from loom.agents.tool_registry import Toolset

        received_tools: list = []

        class SpyBackend:
            async def run(self, prompt, *, tools=None, history=None,
                          agent_id="", max_turns=None):
                received_tools.extend(tools or [])
                return AgentResult(output="done", agent="spy")

        async def my_tool(x: str) -> str:
            """A test tool."""
            return x

        @workflow(name="test_resolve")
        async def wf(ctx: Context, q: str) -> str:
            r = await ctx.agent(q)
            return r.output

        rt = Runtime(agent_backend=SpyBackend())
        rt.toolsets.register(
            Toolset.from_callables("test", [my_tool])
        )

        result = await rt.run(wf, "hello")
        assert result.status.value == "completed"
        assert len(received_tools) == 1

    @pytest.mark.asyncio
    async def test_ctx_agent_selective_toolsets(self) -> None:
        from loom import Context, Runtime, workflow
        from loom.agents.result import AgentResult
        from loom.agents.tool_registry import Toolset

        received_tools: list = []

        class SpyBackend:
            async def run(self, prompt, *, tools=None, history=None,
                          agent_id="", max_turns=None):
                received_tools.clear()
                received_tools.extend(tools or [])
                return AgentResult(output="done", agent="spy")

        async def tool_a() -> str:
            """A."""
            return ""

        async def tool_b() -> str:
            """B."""
            return ""

        @workflow(name="test_selective")
        async def wf(ctx: Context, q: str) -> str:
            r = await ctx.agent(q, toolsets=["only_a"])
            return r.output

        rt = Runtime(agent_backend=SpyBackend())
        rt.toolsets.register(Toolset.from_callables("only_a", [tool_a]))
        rt.toolsets.register(Toolset.from_callables("only_b", [tool_b]))

        await rt.run(wf, "hello")
        assert len(received_tools) == 1


class TestCodingAgentToolRegistry:
    def test_system_prompt_includes_registry_docs(self) -> None:
        from loom import step
        from loom.agents.coding_agent import (
            WorkflowCodingAgent,
        )
        from loom.agents.tool_registry import (
            Toolset,
            ToolsetRegistry,
        )

        @step
        async def my_search(query: str) -> list:
            """Search for things."""
            return []

        class FakeModel:
            model_name = "test"

        registry = ToolsetRegistry()
        registry.register(Toolset.from_steps("demo", [my_search]))

        agent = WorkflowCodingAgent(
            model=FakeModel(), tool_registry=registry
        )
        prompt = agent.build_system_prompt()

        # One line: the toolset exists, and how it is reached. `from_steps`
        # declares no `tools_module`, so this one is callable through
        # ctx.agent() and not importable — which the line has to say, or
        # generated code writes an import that does not exist.
        assert "demo" in prompt
        assert "ctx.agent() only" in prompt

        # Operation names and signatures are what make a toolset block grow
        # with the *size* of an integration rather than its existence, so they
        # are fetched on demand instead. See
        # tests/test_toolset_discovery.py::TestThePromptBlock.
        assert "my_search(query" not in prompt, "signatures leaked into the prompt"
        assert "show_toolset" in prompt, "the prompt must name the way to get detail"
