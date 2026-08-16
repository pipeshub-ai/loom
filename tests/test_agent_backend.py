"""Tests for the AgentBackend protocol and Runtime integration."""

from __future__ import annotations

from importlib.util import find_spec

import pytest


class TestAgentBackendProtocol:
    def test_builtin_backend_conforms(self) -> None:
        from loom.agents.backend import (
            AgentBackend,
            BuiltInBackend,
        )

        class FakeModel:
            model_name = "test"

        backend = BuiltInBackend(model=FakeModel())
        assert isinstance(backend, AgentBackend)

    def test_langchain_backend_conforms(self) -> None:
        from loom.agents.backend import AgentBackend
        from loom.agents.backends.langchain import (
            LangChainBackend,
        )

        backend = LangChainBackend(llm=object())
        assert isinstance(backend, AgentBackend)


class TestRuntimeAgentBackend:
    def test_runtime_accepts_agent_backend(self) -> None:
        from loom import Runtime
        from loom.agents.backend import BuiltInBackend

        class FakeModel:
            model_name = "test"

        backend = BuiltInBackend(model=FakeModel())
        rt = Runtime(agent_backend=backend)
        assert rt.agent_backend is backend

    def test_runtime_default_no_backend(self) -> None:
        from loom import Runtime

        rt = Runtime()
        assert rt.agent_backend is None


class TestCtxAgentPromptOnly:
    @pytest.mark.asyncio
    async def test_prompt_only_uses_backend(self) -> None:
        from loom import Context, Runtime, workflow
        from loom.agents.result import AgentResult

        class FakeBackend:
            supports_history = True

            async def run(self, prompt: str, *, tools=None, history=None,
                          agent_id="", max_turns=None) -> AgentResult:
                return AgentResult(
                    output=f"Response to: {prompt}",
                    agent="fake",
                )

        @workflow(name="test_prompt_agent")
        async def test_wf(ctx: Context, q: str) -> str:
            result = await ctx.agent(f"Search for {q}")
            return result.output

        rt = Runtime(agent_backend=FakeBackend())
        result = await rt.run(test_wf, "AI news")
        assert result.status.value == "completed"
        assert "Response to: Search for AI news" in result.output

    @pytest.mark.asyncio
    async def test_prompt_only_without_backend_raises(self) -> None:
        from loom import Context, Runtime, workflow

        @workflow(name="test_no_backend")
        async def test_wf(ctx: Context, q: str) -> str:
            result = await ctx.agent("Search for stuff")
            return result.output

        rt = Runtime()  # No agent_backend
        result = await rt.run(test_wf, "x")
        assert result.status.value == "failed"

    @pytest.mark.asyncio
    async def test_agent_object_still_works(self) -> None:
        """Backward compat: ctx.agent(Agent(...), input) still works."""
        from loom import Context, Runtime, workflow
        from loom.agents.agent import Agent
        from loom.agents.result import AgentResult

        class FakeExecutor:
            agent_id = "fake"

            async def execute(self, input, **kwargs):
                return AgentResult(
                    output=f"old-style: {input}",
                    agent="fake",
                )

        researcher = Agent(
            name="researcher",
            executor=FakeExecutor(),
        )

        @workflow(name="test_old_style")
        async def test_wf(ctx: Context, q: str) -> str:
            result = await ctx.agent(researcher, q)
            return result.output

        rt = Runtime()
        result = await rt.run(test_wf, "hello")
        assert result.status.value == "completed"
        assert "old-style: hello" in result.output


@pytest.mark.skipif(
    find_spec("langchain") is None, reason="needs the langchain extra"
)
class TestLangChainBackendMocked:
    @pytest.mark.asyncio
    async def test_run_returns_agent_result(self) -> None:
        from unittest.mock import AsyncMock, patch

        from loom.agents.backends.langchain import (
            LangChainBackend,
        )
        from loom.agents.result import AgentResult

        backend = LangChainBackend(llm=object())

        ai_msg = type("AI", (), {
            "content": "Found 3 articles",
            "tool_calls": (),
            "usage_metadata": {"input_tokens": 50, "output_tokens": 30},
        })()
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [ai_msg],
        }

        with patch(
            "langchain.agents.create_agent",
            return_value=mock_graph,
        ):
            result = await backend.run("Find AI articles")
        assert isinstance(result, AgentResult)
        assert result.output == "Found 3 articles"
        assert result.agent == "langchain"
