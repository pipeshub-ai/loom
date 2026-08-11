"""Tests for Phase 10 — Agent Framework Integrations.

Covers: adapter base protocol, all 7 framework adapters,
conformance suite, Direction B tool schema builders.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Base Protocol
# ---------------------------------------------------------------------------


class TestAgentExecutorProtocol:
    def test_protocol_importable(self) -> None:
        from workflow_builder.integrations.base import (
            AgentExecutor,
        )

        assert AgentExecutor is not None

    def test_protocol_is_runtime_checkable(self) -> None:
        from workflow_builder.integrations.base import (
            AgentExecutor,
        )

        # A class with the right signature should match
        class _MockExecutor:
            async def execute(
                self,
                input: str,
                tools: list | None = None,
                output_type: type | None = None,
                settings: dict | None = None,
            ):
                return "ok"

        assert isinstance(_MockExecutor(), AgentExecutor)


# ---------------------------------------------------------------------------
# Adapter classes exist and have execute()
# ---------------------------------------------------------------------------


class TestAdapterImports:
    def test_langgraph_adapter(self) -> None:
        from workflow_builder.integrations.langgraph_adapter import (
            LangGraphExecutor,
        )

        assert hasattr(LangGraphExecutor, "execute")

    def test_pydantic_ai_adapter(self) -> None:
        from workflow_builder.integrations.pydantic_ai_adapter import (
            PydanticAIExecutor,
        )

        assert hasattr(PydanticAIExecutor, "execute")

    def test_openai_agents_adapter(self) -> None:
        from workflow_builder.integrations.openai_agents_adapter import (
            OpenAIAgentsExecutor,
        )

        assert hasattr(OpenAIAgentsExecutor, "execute")

    def test_claude_adapter(self) -> None:
        from workflow_builder.integrations.claude_adapter import (
            ClaudeExecutor,
        )

        assert hasattr(ClaudeExecutor, "execute")

    def test_crewai_adapter(self) -> None:
        from workflow_builder.integrations.crewai_adapter import (
            CrewAIExecutor,
        )

        assert hasattr(CrewAIExecutor, "execute")

    def test_agno_adapter(self) -> None:
        from workflow_builder.integrations.agno_adapter import (
            AgnoExecutor,
        )

        assert hasattr(AgnoExecutor, "execute")

    def test_autogen_adapter(self) -> None:
        from workflow_builder.integrations.autogen_adapter import (
            AutoGenExecutor,
        )

        assert hasattr(AutoGenExecutor, "execute")


# ---------------------------------------------------------------------------
# Direction B — Tool schema builders
# ---------------------------------------------------------------------------


class TestToolSchemaBuilders:
    def test_langgraph_tool_schema(self) -> None:
        from workflow_builder.integrations.langgraph_adapter import (
            workflow_as_langchain_tool,
        )

        schema = workflow_as_langchain_tool(
            None, "lead_outreach", "Run outreach"
        )
        assert schema["name"] == "loom_lead_outreach"
        assert "input_schema" in schema

    def test_pydantic_ai_tool_schema(self) -> None:
        from workflow_builder.integrations.pydantic_ai_adapter import (
            workflow_as_pydantic_tool,
        )

        schema = workflow_as_pydantic_tool(
            None, "crm_sync"
        )
        assert schema["name"] == "loom_crm_sync"

    def test_openai_agents_tool_schema(self) -> None:
        from workflow_builder.integrations.openai_agents_adapter import (
            workflow_as_openai_tool,
        )

        schema = workflow_as_openai_tool(
            None, "etl", "ETL pipeline"
        )
        assert schema["name"] == "loom_etl"
        assert "ETL" in schema["description"]

    def test_claude_tool_schema(self) -> None:
        from workflow_builder.integrations.claude_adapter import (
            workflow_as_claude_tool,
        )

        schema = workflow_as_claude_tool(
            None, "inbox", "Inbox triage"
        )
        assert schema["name"] == "loom_inbox"
        assert "input_schema" in schema

    def test_crewai_tool_schema(self) -> None:
        from workflow_builder.integrations.crewai_adapter import (
            workflow_as_crew_tool,
        )

        schema = workflow_as_crew_tool(
            None, "content", "Content pipeline"
        )
        assert schema["name"] == "loom_content"

    def test_agno_tool_schema(self) -> None:
        from workflow_builder.integrations.agno_adapter import (
            workflow_as_agno_tool,
        )

        schema = workflow_as_agno_tool(None, "meeting")
        assert schema["name"] == "loom_meeting"

    def test_autogen_tool_schema(self) -> None:
        from workflow_builder.integrations.autogen_adapter import (
            workflow_as_autogen_tool,
        )

        schema = workflow_as_autogen_tool(None, "doc_extract")
        assert schema["name"] == "loom_doc_extract"


# ---------------------------------------------------------------------------
# Tool schema structure validation
# ---------------------------------------------------------------------------


class TestToolSchemaStructure:
    """All Direction B schemas must have consistent shape."""

    def _all_schemas(self) -> list[dict]:
        from workflow_builder.integrations.agno_adapter import (
            workflow_as_agno_tool,
        )
        from workflow_builder.integrations.autogen_adapter import (
            workflow_as_autogen_tool,
        )
        from workflow_builder.integrations.claude_adapter import (
            workflow_as_claude_tool,
        )
        from workflow_builder.integrations.crewai_adapter import (
            workflow_as_crew_tool,
        )
        from workflow_builder.integrations.langgraph_adapter import (
            workflow_as_langchain_tool,
        )
        from workflow_builder.integrations.openai_agents_adapter import (
            workflow_as_openai_tool,
        )
        from workflow_builder.integrations.pydantic_ai_adapter import (
            workflow_as_pydantic_tool,
        )

        return [
            workflow_as_langchain_tool(None, "w", "d"),
            workflow_as_pydantic_tool(None, "w"),
            workflow_as_openai_tool(None, "w", "d"),
            workflow_as_claude_tool(None, "w", "d"),
            workflow_as_crew_tool(None, "w", "d"),
            workflow_as_agno_tool(None, "w"),
            workflow_as_autogen_tool(None, "w"),
        ]

    def test_all_have_name(self) -> None:
        for schema in self._all_schemas():
            assert "name" in schema, f"Missing 'name': {schema}"

    def test_all_names_prefixed(self) -> None:
        for schema in self._all_schemas():
            assert schema["name"].startswith("loom_"), (
                f"Name not prefixed: {schema['name']}"
            )

    def test_all_have_description(self) -> None:
        for schema in self._all_schemas():
            assert "description" in schema, (
                f"Missing 'description': {schema}"
            )

    def test_all_have_input_schema(self) -> None:
        for schema in self._all_schemas():
            assert "input_schema" in schema, (
                f"Missing 'input_schema': {schema}"
            )


# ---------------------------------------------------------------------------
# Conformance Suite
# ---------------------------------------------------------------------------


class TestConformanceSuite:
    def test_conformance_importable(self) -> None:
        from workflow_builder.integrations.conformance import (
            ExecutorConformanceSuite,
        )

        assert ExecutorConformanceSuite is not None


# ---------------------------------------------------------------------------
# Adapter constructors
# ---------------------------------------------------------------------------


class TestAdapterConstructors:
    def test_langgraph_executor_init(self) -> None:
        from workflow_builder.integrations.langgraph_adapter import (
            LangGraphExecutor,
        )

        exec_ = LangGraphExecutor(graph=object())
        assert exec_ is not None

    def test_claude_executor_init(self) -> None:
        from workflow_builder.integrations.claude_adapter import (
            ClaudeExecutor,
        )

        exec_ = ClaudeExecutor(
            model="claude-sonnet-4-20250514",
            max_turns=5,
        )
        assert exec_ is not None

    def test_pydantic_ai_executor_init(self) -> None:
        from workflow_builder.integrations.pydantic_ai_adapter import (
            PydanticAIExecutor,
        )

        exec_ = PydanticAIExecutor(agent=object())
        assert exec_ is not None

    def test_openai_agents_executor_init(self) -> None:
        from workflow_builder.integrations.openai_agents_adapter import (
            OpenAIAgentsExecutor,
        )

        exec_ = OpenAIAgentsExecutor(agent=object())
        assert exec_ is not None

    def test_crewai_executor_init(self) -> None:
        from workflow_builder.integrations.crewai_adapter import (
            CrewAIExecutor,
        )

        exec_ = CrewAIExecutor(crew=object())
        assert exec_ is not None

    def test_agno_executor_init(self) -> None:
        from workflow_builder.integrations.agno_adapter import (
            AgnoExecutor,
        )

        exec_ = AgnoExecutor(agent=object())
        assert exec_ is not None

    def test_autogen_executor_init(self) -> None:
        from workflow_builder.integrations.autogen_adapter import (
            AutoGenExecutor,
        )

        exec_ = AutoGenExecutor(team=object())
        assert exec_ is not None
