"""Tests for Phase 10 — Agent Framework Integrations.

Covers: adapter base protocol, all 7 framework adapters,
conformance suite, Direction B tool schema builders.
"""

from __future__ import annotations

import pytest
from _pytest.outcomes import Failed

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


# ---------------------------------------------------------------------------
# Structured output across every adapter
# ---------------------------------------------------------------------------


class TestCoerceOutput:
    """One place that knows how to get a typed answer out of prose.

    Seven adapters accepted ``output_type`` and none applied it, so a caller
    asking for a model got a string and no error. The coercion lives here now,
    which is also why fixing it was one change rather than seven.
    """

    def _answer(self):
        from workflow_builder.integrations.conformance import ConformanceAnswer

        return ConformanceAnswer

    def test_none_passes_through_untouched(self) -> None:
        from workflow_builder.integrations.structured import coerce_output

        assert coerce_output("anything", None) == "anything"

    def test_an_instance_is_returned_as_is(self) -> None:
        from workflow_builder.integrations.structured import coerce_output

        answer = self._answer()(value=1)
        assert coerce_output(answer, self._answer()) is answer

    def test_a_dict_is_validated(self) -> None:
        from workflow_builder.integrations.structured import coerce_output

        assert coerce_output({"value": 3}, self._answer()).value == 3

    def test_bare_json_is_parsed(self) -> None:
        from workflow_builder.integrations.structured import coerce_output

        assert coerce_output('{"value": 4}', self._answer()).value == 4

    def test_a_fenced_block_is_parsed(self) -> None:
        """What a model actually returns, whatever the prompt asked for."""
        from workflow_builder.integrations.structured import coerce_output

        text = 'Sure!\n```json\n{"value": 5, "label": "x"}\n```\nHope that helps.'
        answer = coerce_output(text, self._answer())
        assert (answer.value, answer.label) == (5, "x")

    def test_json_embedded_in_prose_is_found(self) -> None:
        from workflow_builder.integrations.structured import coerce_output

        assert coerce_output('here: {"value": 6} ok?', self._answer()).value == 6

    def test_a_nested_object_is_not_truncated(self) -> None:
        """A first-closing-brace scan would cut this in half."""
        from workflow_builder.integrations.structured import extract_json

        assert extract_json('x {"a": {"b": 1}, "c": 2} y') == {"a": {"b": 1}, "c": 2}

    def test_a_framework_wrapper_is_unwrapped(self) -> None:
        """Frameworks return their own envelope around the answer."""
        from workflow_builder.integrations.structured import coerce_output

        class Wrapper:
            def __init__(self) -> None:
                self.output = {"value": 8}

        assert coerce_output(Wrapper(), self._answer()).value == 8

    def test_prose_with_no_json_raises_rather_than_degrading(self) -> None:
        """The whole point. Silently returning the string is the original bug."""
        from workflow_builder.core.exceptions import ValidationError
        from workflow_builder.integrations.structured import coerce_output

        with pytest.raises(ValidationError, match="no JSON"):
            coerce_output("I could not do that", self._answer())

    def test_json_of_the_wrong_shape_raises(self) -> None:
        from workflow_builder.core.exceptions import ValidationError
        from workflow_builder.integrations.structured import coerce_output

        with pytest.raises(ValidationError, match="does not fit"):
            coerce_output('{"wrong": 1}', self._answer())

    @pytest.mark.parametrize(
        "module",
        [
            "agno_adapter",
            "autogen_adapter",
            "claude_adapter",
            "crewai_adapter",
            "langgraph_adapter",
            "openai_agents_adapter",
            "pydantic_ai_adapter",
        ],
    )
    def test_every_adapter_applies_it(self, module: str) -> None:
        """A drift guard over the whole family.

        Accepting ``output_type`` in a signature and never referencing it in the
        body is exactly what shipped, in all seven, for as long as they existed.
        """
        import importlib
        import inspect

        loaded = importlib.import_module(f"workflow_builder.integrations.{module}")
        source = inspect.getsource(loaded)

        assert "output_type" in source, module
        assert "coerce_output(" in source, (
            f"{module} accepts output_type but never applies it"
        )


class TestTheConformanceSuiteActuallyChecks:
    """The suite is the thing that keeps the adapters honest, so test it too.

    An empty suite is worse than none: it looks like coverage. This runs it
    against two fakes — one that honours the contract and one that does not —
    and asserts it can tell them apart.
    """

    def _suite(self, executor):
        from workflow_builder.integrations.conformance import ExecutorConformanceSuite

        suite = ExecutorConformanceSuite()
        suite.executor = executor
        return suite

    class Honest:
        """Uses the shared coercion, as an adapter should."""

        async def execute(self, *, input, tools=None, output_type=None, settings=None):
            from workflow_builder.integrations.structured import coerce_output

            reply = '{"value": 7}' if "7" in input else "some prose"
            return coerce_output(reply, output_type)

    class Dropper:
        """Accepts output_type and ignores it — the original defect."""

        async def execute(self, *, input, tools=None, output_type=None, settings=None):
            return "some prose"

    async def test_it_passes_an_adapter_that_honours_the_contract(self) -> None:
        suite = self._suite(self.Honest())

        await suite.test_it_returns_something()
        await suite.test_it_accepts_settings()
        await suite.test_it_accepts_tools()
        await suite.test_a_declared_output_type_is_honoured()
        await suite.test_an_unmeetable_output_type_raises()

    async def test_it_fails_an_adapter_that_drops_the_output_type(self) -> None:
        suite = self._suite(self.Dropper())

        with pytest.raises(AssertionError, match="did not apply it"):
            await suite.test_a_declared_output_type_is_honoured()

    async def test_it_fails_an_adapter_that_degrades_instead_of_raising(self) -> None:
        suite = self._suite(self.Dropper())

        with pytest.raises(Failed):
            await suite.test_an_unmeetable_output_type_raises()
