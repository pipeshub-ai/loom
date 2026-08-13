"""Tests for the LangChain → LOOM Agent bridge adapter."""

from __future__ import annotations

import asyncio

import pytest


class TestLangChainAgentExecutorProtocol:
    def test_has_agent_id(self) -> None:
        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        exec_ = LangChainAgentExecutor(
            graph=None, agent_id="test"
        )
        assert exec_.agent_id == "test"

    def test_default_agent_id(self) -> None:
        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        exec_ = LangChainAgentExecutor(graph=None)
        assert exec_.agent_id == "langchain"

    def test_conforms_to_protocol(self) -> None:
        from workflow_builder.agents.executor import (
            AgentExecutor,
        )
        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        exec_ = LangChainAgentExecutor(
            graph=None, agent_id="test"
        )
        assert isinstance(exec_, AgentExecutor)

    def test_has_execute_method(self) -> None:
        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        exec_ = LangChainAgentExecutor(graph=None)
        assert hasattr(exec_, "execute")
        assert asyncio.iscoroutinefunction(exec_.execute)


class TestLangChainAgentExecutorMocked:
    @pytest.mark.asyncio
    async def test_execute_returns_agent_result(self) -> None:
        from unittest.mock import AsyncMock

        from workflow_builder.agents.result import AgentResult
        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        # Mock a LangGraph-style compiled graph
        mock_graph = AsyncMock()

        # Simulate LangChain message response
        ai_msg = type("FakeAIMessage", (), {
            "content": "Here are 3 AI articles...",
            "tool_calls": (),
            "usage_metadata": {"input_tokens": 100, "output_tokens": 50},
        })()
        human_msg = type("FakeHumanMessage", (), {
            "content": "Find AI articles",
        })()

        mock_graph.ainvoke.return_value = {
            "messages": [human_msg, ai_msg],
        }

        exec_ = LangChainAgentExecutor(
            graph=mock_graph, agent_id="test_agent"
        )
        result = await exec_.execute("Find AI articles")

        assert isinstance(result, AgentResult)
        assert result.output == "Here are 3 AI articles..."
        assert result.agent == "test_agent"
        assert result.turns == 1
        assert result.usage.input_tokens == 100
        assert result.usage.output_tokens == 50

    @pytest.mark.asyncio
    async def test_execute_handles_empty_messages(self) -> None:
        from unittest.mock import AsyncMock

        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": []}

        exec_ = LangChainAgentExecutor(graph=mock_graph)
        result = await exec_.execute("test")

        assert result.output == ""
        assert result.turns == 0

    @pytest.mark.asyncio
    async def test_execute_counts_tool_calls(self) -> None:
        from unittest.mock import AsyncMock

        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        class FakeAIWithTools:
            content = ""
            tool_calls = (
                {"name": "search", "args": {}},
                {"name": "fetch", "args": {}},
            )
            usage_metadata = None

        class FakeAIFinal:
            content = "Summary of findings"
            tool_calls = ()
            usage_metadata = None

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [FakeAIWithTools(), FakeAIFinal()],
        }

        exec_ = LangChainAgentExecutor(graph=mock_graph)
        result = await exec_.execute("test")

        assert result.output == "Summary of findings"
        assert result.turns == 2


class TestLangChainAgentWithLOOMAgent:
    @pytest.mark.asyncio
    async def test_agent_uses_langchain_executor(self) -> None:
        from unittest.mock import AsyncMock

        from workflow_builder.agents.agent import Agent
        from workflow_builder.integrations.langgraph_agent import (
            LangChainAgentExecutor,
        )

        class FakeMsg:
            content = "Done!"
            tool_calls = ()
            usage_metadata = None

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [FakeMsg()],
        }

        agent = Agent(
            name="test_researcher",
            executor=LangChainAgentExecutor(
                graph=mock_graph, agent_id="test"
            ),
        )

        result = await agent("Search for AI news")
        assert result.output == "Done!"
        assert result.agent == "test"


class TestLangChainToolDocs:
    def test_docs_exist(self) -> None:
        from workflow_builder.integrations.langchain_tools_docs import (
            LANGCHAIN_TOOL_DOCS,
        )

        assert len(LANGCHAIN_TOOL_DOCS) > 100
        assert "ctx.agent" in LANGCHAIN_TOOL_DOCS
        assert "web" in LANGCHAIN_TOOL_DOCS.lower()
        assert "result.output" in LANGCHAIN_TOOL_DOCS

    async def test_registered_in_coding_tools(self) -> None:
        from workflow_builder.agents.coding_tools import get_tool_docs

        assert "ctx.agent" in await get_tool_docs.fn("langchain")
