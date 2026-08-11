"""Tests for Agent, AgentExecutor, runner, and mock system."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from workflow_builder.agents.agent import Agent, PersistenceClass
from workflow_builder.agents.executor import AgentExecutor
from workflow_builder.agents.guardrails import allow, guardrail, reject, tripwire
from workflow_builder.agents.limits import UsageLimits
from workflow_builder.agents.messages import ToolCall
from workflow_builder.agents.models import (
    FinishReason,
)
from workflow_builder.agents.output import FINAL_OUTPUT_TOOL
from workflow_builder.agents.result import AgentResult
from workflow_builder.agents.runner import BuiltInAgentRuntime
from workflow_builder.agents.tools import tool
from workflow_builder.core.exceptions import (
    GuardrailTripwire,
    ModelBehaviorError,
    UsageLimitExceeded,
)
from workflow_builder.testing.mock import MockModelProvider, mock_response

# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


class TestAgentConstruction:
    def test_basic_agent(self) -> None:
        agent = Agent(name="test")
        assert agent.name == "test"
        assert agent.persistence is PersistenceClass.EPHEMERAL
        assert repr(agent) == "<Agent test persistence=ephemeral>"

    def test_agent_with_persistence(self) -> None:
        agent = Agent(name="chat", persistence=PersistenceClass.SESSION)
        assert agent.persistence is PersistenceClass.SESSION

    def test_with_settings(self) -> None:
        agent = Agent(name="test", instructions="You are helpful.")
        clone = agent.with_settings(name="clone", instructions="Be concise.")
        assert clone.name == "clone"
        assert clone.instructions == "Be concise."
        assert agent.name == "test"  # original unchanged


# ---------------------------------------------------------------------------
# MockModelProvider
# ---------------------------------------------------------------------------


class TestMockModelProvider:
    @pytest.mark.asyncio
    async def test_returns_scripted_responses(self) -> None:
        provider = MockModelProvider(responses=[
            mock_response("First"),
            mock_response("Second"),
        ])
        from workflow_builder.agents.messages import user
        from workflow_builder.agents.models import ModelRequest

        r1 = await provider.complete(ModelRequest(messages=[user("hi")]))
        assert r1.message.text() == "First"
        r2 = await provider.complete(ModelRequest(messages=[user("again")]))
        assert r2.message.text() == "Second"
        assert provider.call_count == 2
        assert len(provider.requests) == 2

    @pytest.mark.asyncio
    async def test_exhausted_responses(self) -> None:
        provider = MockModelProvider(responses=[mock_response("Only one")])
        from workflow_builder.agents.messages import user
        from workflow_builder.agents.models import ModelRequest

        await provider.complete(ModelRequest(messages=[user("1")]))
        r2 = await provider.complete(ModelRequest(messages=[user("2")]))
        assert "no more responses" in r2.message.text().lower()

    def test_reset(self) -> None:
        provider = MockModelProvider(responses=[mock_response("Hi")])
        provider._call_count = 5
        provider.reset()
        assert provider.call_count == 0
        assert provider.requests == []


# ---------------------------------------------------------------------------
# BuiltInAgentRuntime — basic text output
# ---------------------------------------------------------------------------


class TestAgentRunnerTextOutput:
    @pytest.mark.asyncio
    async def test_simple_text_response(self) -> None:
        provider = MockModelProvider(responses=[
            mock_response("The answer is 42."),
        ])
        agent = Agent(name="simple", model=provider, instructions="Answer questions.")
        result = await agent("What is the answer?")
        assert isinstance(result, AgentResult)
        assert result.output == "The answer is 42."
        assert result.turns == 1
        assert result.agent == "simple"

    @pytest.mark.asyncio
    async def test_no_model_raises(self) -> None:
        agent = Agent(name="no-model")
        with pytest.raises(ModelBehaviorError, match="no model"):
            await agent("hello")


# ---------------------------------------------------------------------------
# BuiltInAgentRuntime — tool use
# ---------------------------------------------------------------------------


class TestAgentRunnerToolUse:
    @pytest.mark.asyncio
    async def test_tool_call_and_response(self) -> None:
        @tool
        async def get_weather(city: str) -> str:
            """Get weather for a city.

            Args:
                city: The city name.
            """
            return f"Sunny in {city}"

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[ToolCall(id="c1", name="get_weather", arguments={"city": "Paris"})]
            ),
            mock_response("It's sunny in Paris!"),
        ])
        agent = Agent(name="weather", model=provider, tools=[get_weather])
        result = await agent("Weather in Paris?")
        assert result.output == "It's sunny in Paris!"
        assert result.turns == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_weather"

    @pytest.mark.asyncio
    async def test_unknown_tool_handled(self) -> None:
        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})]
            ),
            mock_response("Okay, I'll do something else."),
        ])
        agent = Agent(name="test", model=provider)
        result = await agent("do something")
        assert result.turns == 2
        # The model received an error message about unknown tool
        req2 = provider.requests[1]
        tool_msgs = [m for m in req2.messages if m.role.value == "tool"]
        assert any("Unknown tool" in m.text() for m in tool_msgs)


# ---------------------------------------------------------------------------
# BuiltInAgentRuntime — structured output via tool
# ---------------------------------------------------------------------------


class TestAgentRunnerStructuredOutput:
    @pytest.mark.asyncio
    async def test_structured_output_via_final_tool(self) -> None:
        class Answer(BaseModel):
            value: int
            explanation: str

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[
                    ToolCall(
                        id="f1",
                        name=FINAL_OUTPUT_TOOL,
                        arguments={"value": 42, "explanation": "It's the answer"},
                    )
                ]
            ),
        ])
        agent = Agent(name="structured", model=provider, output_type=Answer)
        result = await agent("What's the answer?")
        assert isinstance(result.output, Answer)
        assert result.output.value == 42
        assert result.output.explanation == "It's the answer"


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_max_turns_exceeded(self) -> None:
        # Use LENGTH finish reason to force the agent to keep looping
        provider = MockModelProvider(responses=[
            mock_response("thinking...", finish_reason=FinishReason.LENGTH),
            mock_response("still thinking...", finish_reason=FinishReason.LENGTH),
            mock_response("almost there...", finish_reason=FinishReason.LENGTH),
        ])
        agent = Agent(
            name="slow",
            model=provider,
            limits=UsageLimits(max_turns=2),
        )
        with pytest.raises(UsageLimitExceeded, match="max_turns"):
            await agent("take your time")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


class TestGuardrails:
    @pytest.mark.asyncio
    async def test_guardrail_rejects_tool_call(self) -> None:
        @tool
        async def dangerous_action(target: str) -> str:
            """Dangerous action.

            Args:
                target: The target.
            """
            return f"destroyed {target}"

        @guardrail
        def no_destruction(arguments: dict, tool_name: str):
            if "destroy" in str(arguments).lower():
                return reject("Destructive actions are not allowed.")
            return allow()

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="dangerous_action",
                        arguments={"target": "destroy everything"},
                    )
                ]
            ),
            mock_response("I won't do that."),
        ])
        agent = Agent(
            name="safe",
            model=provider,
            tools=[dangerous_action],
            guardrails=[no_destruction],
        )
        result = await agent("destroy it all")
        assert result.turns == 2
        # Guardrail message was sent back
        req2 = provider.requests[1]
        tool_msgs = [m for m in req2.messages if m.role.value == "tool"]
        assert any("rejected" in m.text().lower() for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_guardrail_tripwire_aborts(self) -> None:
        @tool
        async def any_action(data: str) -> str:
            """Any action.

            Args:
                data: The data.
            """
            return data

        @guardrail
        def always_tripwire(arguments: dict, tool_name: str):
            return tripwire("Policy violation detected.")

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[ToolCall(id="c1", name="any_action", arguments={"data": "test"})]
            ),
        ])
        agent = Agent(
            name="strict",
            model=provider,
            tools=[any_action],
            guardrails=[always_tripwire],
        )
        with pytest.raises(GuardrailTripwire, match="Policy violation"):
            await agent("do something")


# ---------------------------------------------------------------------------
# AgentExecutor protocol
# ---------------------------------------------------------------------------


class TestAgentExecutorProtocol:
    def test_builtin_satisfies_protocol(self) -> None:
        provider = MockModelProvider()
        agent = Agent(name="test", model=provider)
        runtime = BuiltInAgentRuntime(agent)
        assert isinstance(runtime, AgentExecutor)
        assert runtime.agent_id == "test"


# ---------------------------------------------------------------------------
# Agent in workflow
# ---------------------------------------------------------------------------


class TestAgentInWorkflow:
    @pytest.mark.asyncio
    async def test_agent_step_in_workflow(self) -> None:
        from workflow_builder import Context, Runtime, workflow

        provider = MockModelProvider(responses=[
            mock_response("Triaged as P2."),
        ])
        triage_agent = Agent(
            name="triage",
            model=provider,
            instructions="Triage the ticket.",
        )

        @workflow
        async def support_wf(ctx: Context, ticket: str) -> str:
            result = await ctx.agent(triage_agent, ticket)
            return result.text()

        rt = Runtime()
        result = await rt.run(support_wf, "My login is broken")
        assert result.output == "Triaged as P2."
