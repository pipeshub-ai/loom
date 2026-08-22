"""Model provider translation, tested without touching a network.

A provider's whole job is translating between LOOM's neutral message format and
one vendor's wire format, in both directions. That is exactly what breaks
silently — a tool call whose arguments arrive as a JSON string instead of a
dict, a system prompt sent as a message to an API that wants it as config — so
these tests drive real request construction against fake clients and assert on
the payloads.

The vendor SDKs are optional, so each group skips if its SDK is absent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from loom.agents.messages import (
    ToolCall,
    assistant,
    system,
    tool_result,
    user,
)
from loom.agents.models import (
    FinishReason,
    ModelRequest,
    ModelSettings,
    ToolSchema,
    estimate_cost,
)
from loom.core.models import Usage

SEARCH_TOOL = ToolSchema(
    name="search",
    description="Search the web.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


def _conversation() -> list:
    """A transcript covering every role, including a completed tool round-trip."""
    return [
        system("You are terse."),
        user("Find me something."),
        assistant(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="search", arguments={"query": "loom"})],
        ),
        tool_result("call_1", '{"hits": 3}', name="search"),
        user("Summarise that."),
    ]


# ---------------------------------------------------------------------------
# Pricing (vendor-agnostic)
# ---------------------------------------------------------------------------


class TestPricing:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("gpt-4.1", 2.00),
            ("gpt-4.1-mini", 0.40),
            # Dated ids must resolve to their family, not a shorter prefix.
            ("gpt-4.1-mini-2025-04-14", 0.40),
            ("o3", 2.00),
            ("o3-mini", 1.10),
            ("gemini-2.5-flash", 0.30),
            ("gemini-2.5-flash-lite", 0.10),
            ("claude-sonnet-5", 2.00),
            ("claude-sonnet-4-6", 3.00),
            ("gpt-5.6-luna", 0.20),
        ],
    )
    def test_longest_prefix_wins(self, model: str, expected: float) -> None:
        """First-match ordering priced gpt-4.1-mini as gpt-4.1 — 5x too much."""
        cost = estimate_cost(model, Usage(input_tokens=1_000_000))
        assert cost == pytest.approx(expected)

    def test_the_default_openai_model_is_priced(self) -> None:
        """A default that reports zero cost makes budgets unenforceable."""
        from loom.agents.providers import OpenAIProvider

        model = OpenAIProvider(api_key="x").model_name
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

        assert estimate_cost(model, usage) == pytest.approx(2.00)  # 0.20 + 1.80

    def test_sibling_models_are_not_given_lunas_price(self) -> None:
        """A sibling is priced from its own published rate or not at all —
        inheriting luna's by prefix would make estimate_cost confidently wrong,
        and luna is an order of magnitude cheaper than either sibling."""
        usage = Usage(input_tokens=1_000_000)
        assert estimate_cost("gpt-5.6-sol", usage) == 0.0  # rate not on file
        assert estimate_cost("gpt-5.6-terra", usage) == pytest.approx(2.00)

    def test_a_dated_luna_id_still_resolves(self) -> None:
        cost = estimate_cost("gpt-5.6-luna-2026-07-01", Usage(input_tokens=1_000_000))
        assert cost == pytest.approx(0.20)

    def test_unknown_model_is_free_rather_than_wrong(self) -> None:
        assert estimate_cost("some-local-llama", Usage(input_tokens=10_000)) == 0.0

    def test_cached_input_is_discounted(self) -> None:
        full = estimate_cost("gpt-4.1", Usage(input_tokens=1_000_000))
        cached = estimate_cost(
            "gpt-4.1", Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000)
        )
        assert cached < full


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

pytest.importorskip("openai", reason="needs the openai extra")


class FakeOpenAIResponse:
    """Mimics the attribute shape of ``ChatCompletion``."""

    def __init__(self, message: Any, finish_reason: str = "stop") -> None:
        self.choices = [type("Choice", (), {"message": message, "finish_reason": finish_reason})]
        self.model = "gpt-4.1-2025-04-14"
        self.usage = type(
            "U",
            (),
            {
                "prompt_tokens": 120,
                "completion_tokens": 34,
                "prompt_tokens_details": type("D", (), {"cached_tokens": 20}),
                "completion_tokens_details": type("C", (), {"reasoning_tokens": 8}),
            },
        )


def _openai_message(content: str | None = None, tool_calls: list | None = None) -> Any:
    return type("M", (), {"content": content, "tool_calls": tool_calls or []})


def _openai_tool_call(call_id: str, name: str, arguments: str) -> Any:
    return type(
        "TC",
        (),
        {"id": call_id, "function": type("F", (), {"name": name, "arguments": arguments})},
    )


@pytest.fixture
def openai_provider():
    """A provider whose client records the kwargs it was called with."""
    from loom.agents.providers import OpenAIProvider

    provider = OpenAIProvider(model_name="gpt-4.1", api_key="test")
    captured: dict[str, Any] = {}
    reply = FakeOpenAIResponse(_openai_message("done"))

    async def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return captured.get("_reply", reply)

    provider._client = type(
        "C", (), {"chat": type("Chat", (), {"completions": type("Comp", (), {"create": create})})}
    )
    return provider, captured


class TestOpenAIRequest:
    async def test_system_stays_a_message(self, openai_provider) -> None:
        provider, captured = openai_provider
        await provider.complete(ModelRequest(messages=_conversation()))

        roles = [m["role"] for m in captured["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "user"]

    async def test_tool_calls_serialize_arguments_as_json_text(
        self, openai_provider
    ) -> None:
        """OpenAI carries arguments as a string; sending a dict is a 400."""
        provider, captured = openai_provider
        await provider.complete(ModelRequest(messages=_conversation()))

        call = captured["messages"][2]["tool_calls"][0]
        assert call["type"] == "function"
        assert isinstance(call["function"]["arguments"], str)
        assert json.loads(call["function"]["arguments"]) == {"query": "loom"}

    async def test_tool_result_carries_the_call_id(self, openai_provider) -> None:
        provider, captured = openai_provider
        await provider.complete(ModelRequest(messages=_conversation()))

        assert captured["messages"][3]["tool_call_id"] == "call_1"

    async def test_tools_are_wrapped_in_a_function_envelope(
        self, openai_provider
    ) -> None:
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(messages=[user("hi")], tools=[SEARCH_TOOL])
        )

        tool = captured["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "search"
        assert tool["function"]["parameters"]["properties"]["query"]["type"] == "string"

    async def test_strict_tools_tighten_the_schema(self, openai_provider) -> None:
        provider, captured = openai_provider
        strict = SEARCH_TOOL.model_copy(update={"strict": True})
        await provider.complete(ModelRequest(messages=[user("hi")], tools=[strict]))

        params = captured["tools"][0]["function"]["parameters"]
        assert captured["tools"][0]["function"]["strict"] is True
        assert params["additionalProperties"] is False
        assert params["required"] == ["query"]

    async def test_settings_are_forwarded(self, openai_provider) -> None:
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")],
                settings=ModelSettings(temperature=0.2, top_p=0.9, stop=["END"], seed=7),
            )
        )

        assert captured["temperature"] == 0.2
        assert captured["top_p"] == 0.9
        assert captured["stop"] == ["END"]
        assert captured["seed"] == 7

    async def test_gpt_5_6_gets_reasoning_effort_none_with_tools(
        self, openai_provider
    ) -> None:
        """Verified live: the gpt-5.6 generation 400s on function tools unless
        reasoning_effort is explicitly "none"."""
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")], tools=[SEARCH_TOOL], model="gpt-5.6-luna"
            )
        )

        assert captured["reasoning_effort"] == "none"

    async def test_that_override_does_not_apply_without_tools(
        self, openai_provider
    ) -> None:
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(messages=[user("hi")], model="gpt-5.6-luna")
        )

        assert "reasoning_effort" not in captured

    async def test_an_explicit_reasoning_effort_wins(self, openai_provider) -> None:
        """The override is a default, not a policy."""
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")],
                tools=[SEARCH_TOOL],
                model="gpt-5.6-luna",
                settings=ModelSettings(reasoning_effort="high"),
            )
        )

        assert captured["reasoning_effort"] == "high"

    @pytest.mark.parametrize("model", ["gpt-5", "gpt-5.4", "gpt-5.5", "gpt-4.1"])
    async def test_other_models_are_left_alone(
        self, openai_provider, model: str
    ) -> None:
        """gpt-5 and gpt-4.1 *reject* reasoning_effort="none", so the override
        must stay scoped to the generation that needs it."""
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(messages=[user("hi")], tools=[SEARCH_TOOL], model=model)
        )

        assert "reasoning_effort" not in captured

    async def test_reasoning_models_use_a_different_token_ceiling(
        self, openai_provider
    ) -> None:
        """o-series rejects max_tokens and the sampling knobs outright."""
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")],
                model="o3-mini",
                settings=ModelSettings(temperature=0.5, max_tokens=100),
            )
        )

        assert captured["max_completion_tokens"] == 100
        assert "max_tokens" not in captured
        assert "temperature" not in captured

    async def test_tool_choice_by_name_becomes_a_function_selector(
        self, openai_provider
    ) -> None:
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")],
                tools=[SEARCH_TOOL],
                settings=ModelSettings(tool_choice="search"),
            )
        )

        assert captured["tool_choice"] == {
            "type": "function",
            "function": {"name": "search"},
        }

    async def test_tool_choice_keyword_passes_through(self, openai_provider) -> None:
        provider, captured = openai_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")],
                tools=[SEARCH_TOOL],
                settings=ModelSettings(tool_choice="required"),
            )
        )
        assert captured["tool_choice"] == "required"


class TestOpenAIResponse:
    async def test_text_reply(self, openai_provider) -> None:
        provider, captured = openai_provider
        captured["_reply"] = FakeOpenAIResponse(_openai_message("hello there"))

        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.message.content == "hello there"
        assert result.finish_reason is FinishReason.STOP
        assert result.model == "gpt-4.1-2025-04-14"

    async def test_usage_including_cached_and_reasoning(self, openai_provider) -> None:
        provider, _ = openai_provider
        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.usage.input_tokens == 120
        assert result.usage.output_tokens == 34
        assert result.usage.cached_input_tokens == 20
        assert result.usage.reasoning_tokens == 8

    async def test_tool_call_arguments_are_parsed_back_to_a_dict(
        self, openai_provider
    ) -> None:
        provider, captured = openai_provider
        captured["_reply"] = FakeOpenAIResponse(
            _openai_message(None, [_openai_tool_call("c1", "search", '{"query": "x"}')]),
            finish_reason="tool_calls",
        )

        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.finish_reason is FinishReason.TOOL_CALLS
        assert result.message.tool_calls[0].arguments == {"query": "x"}

    async def test_malformed_arguments_do_not_raise(self, openai_provider) -> None:
        """A bad-JSON tool call becomes a tool error the model can recover from."""
        provider, captured = openai_provider
        captured["_reply"] = FakeOpenAIResponse(
            _openai_message(None, [_openai_tool_call("c1", "search", "{not json")]),
            finish_reason="tool_calls",
        )

        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.message.tool_calls[0].arguments == {"_raw": "{not json"}

    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("stop", FinishReason.STOP),
            ("tool_calls", FinishReason.TOOL_CALLS),
            ("length", FinishReason.LENGTH),
            ("content_filter", FinishReason.CONTENT_FILTER),
            ("something_new", FinishReason.STOP),
        ],
    )
    async def test_finish_reasons_map(
        self, openai_provider, reason: str, expected: FinishReason
    ) -> None:
        provider, captured = openai_provider
        captured["_reply"] = FakeOpenAIResponse(_openai_message("x"), finish_reason=reason)

        result = await provider.complete(ModelRequest(messages=[user("hi")]))
        assert result.finish_reason is expected


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

pytest.importorskip("google.genai", reason="needs the gemini extra")


class FakeGeminiResponse:
    """Mimics the attribute shape of ``GenerateContentResponse``."""

    def __init__(self, parts: list, finish_reason: str = "FinishReason.STOP") -> None:
        self.candidates = [
            type(
                "Cand",
                (),
                {
                    "content": type("Content", (), {"parts": parts}),
                    "finish_reason": finish_reason,
                },
            )
        ]
        self.model_version = "gemini-2.5-pro-001"
        self.usage_metadata = type(
            "U",
            (),
            {
                "prompt_token_count": 90,
                "candidates_token_count": 12,
                "cached_content_token_count": 5,
                "thoughts_token_count": 3,
            },
        )


def _text_part(text: str) -> Any:
    return type("P", (), {"text": text, "function_call": None})


def _call_part(name: str, args: dict) -> Any:
    return type(
        "P", (), {"text": None, "function_call": type("FC", (), {"name": name, "args": args})}
    )


@pytest.fixture
def gemini_provider():
    from loom.agents.providers import GeminiProvider

    captured: dict[str, Any] = {}
    reply = FakeGeminiResponse([_text_part("done")])

    async def generate_content(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return captured.get("_reply", reply)

    models = type("M", (), {"generate_content": generate_content})
    fake_client = type("C", (), {"aio": type("Aio", (), {"models": models})})
    return GeminiProvider(model_name="gemini-2.5-pro", client=fake_client), captured


class TestGeminiRequest:
    async def test_system_prompt_becomes_configuration(self, gemini_provider) -> None:
        """Gemini takes the system prompt as config, not as a turn."""
        provider, captured = gemini_provider
        await provider.complete(ModelRequest(messages=_conversation()))

        assert captured["config"]["system_instruction"] == "You are terse."
        roles = [c["role"] for c in captured["contents"]]
        assert "system" not in roles

    async def test_assistant_turns_use_the_model_role(self, gemini_provider) -> None:
        provider, captured = gemini_provider
        await provider.complete(ModelRequest(messages=_conversation()))

        assert [c["role"] for c in captured["contents"]] == [
            "user",
            "model",
            "user",
            "user",
        ]

    async def test_tool_result_is_keyed_by_function_name_not_call_id(
        self, gemini_provider
    ) -> None:
        """The sharp edge: LOOM tracks calls by id, Gemini responds by name."""
        provider, captured = gemini_provider
        await provider.complete(ModelRequest(messages=_conversation()))

        response_part = captured["contents"][2]["parts"][0]["function_response"]
        assert response_part["name"] == "search"
        assert response_part["response"] == {"hits": 3}

    async def test_non_json_tool_output_is_wrapped(self, gemini_provider) -> None:
        """Gemini requires an object; a bare string is rejected."""
        provider, captured = gemini_provider
        await provider.complete(
            ModelRequest(
                messages=[
                    assistant(
                        tool_calls=[ToolCall(id="c1", name="search", arguments={})]
                    ),
                    tool_result("c1", "plain text", name="search"),
                ]
            )
        )

        part = captured["contents"][1]["parts"][0]["function_response"]
        assert part["response"] == {"result": "plain text"}

    async def test_function_declarations_are_built(self, gemini_provider) -> None:
        provider, captured = gemini_provider
        await provider.complete(
            ModelRequest(messages=[user("hi")], tools=[SEARCH_TOOL])
        )

        declaration = captured["config"]["tools"][0]["function_declarations"][0]
        assert declaration["name"] == "search"
        assert declaration["parameters"]["properties"]["query"]["type"] == "string"

    async def test_unsupported_schema_keywords_are_stripped(
        self, gemini_provider
    ) -> None:
        """Pydantic emits $defs/additionalProperties; Gemini 400s on them."""
        provider, captured = gemini_provider
        noisy = ToolSchema(
            name="noisy",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "$defs": {"Inner": {"type": "object"}},
                "properties": {
                    "nested": {"type": "object", "additionalProperties": False},
                },
            },
        )
        await provider.complete(ModelRequest(messages=[user("hi")], tools=[noisy]))

        params = captured["config"]["tools"][0]["function_declarations"][0]["parameters"]
        assert "additionalProperties" not in params
        assert "$defs" not in params
        # Stripped recursively, not just at the top level.
        assert "additionalProperties" not in params["properties"]["nested"]

    async def test_a_parameterless_tool_omits_the_parameters_key(
        self, gemini_provider
    ) -> None:
        """An empty parameter object is rejected outright."""
        provider, captured = gemini_provider
        await provider.complete(
            ModelRequest(messages=[user("hi")], tools=[ToolSchema(name="ping")])
        )

        declaration = captured["config"]["tools"][0]["function_declarations"][0]
        assert "parameters" not in declaration

    async def test_settings_are_forwarded(self, gemini_provider) -> None:
        provider, captured = gemini_provider
        await provider.complete(
            ModelRequest(
                messages=[user("hi")],
                settings=ModelSettings(temperature=0.3, max_tokens=256, stop=["END"]),
            )
        )

        config = captured["config"]
        assert config["temperature"] == 0.3
        assert config["max_output_tokens"] == 256
        assert config["stop_sequences"] == ["END"]


class TestGeminiResponse:
    async def test_text_reply(self, gemini_provider) -> None:
        provider, _ = gemini_provider
        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.message.content == "done"
        assert result.model == "gemini-2.5-pro-001"

    async def test_usage_is_normalised(self, gemini_provider) -> None:
        provider, _ = gemini_provider
        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.usage.input_tokens == 90
        assert result.usage.output_tokens == 12
        assert result.usage.cached_input_tokens == 5
        assert result.usage.reasoning_tokens == 3

    async def test_function_calls_are_extracted(self, gemini_provider) -> None:
        provider, captured = gemini_provider
        captured["_reply"] = FakeGeminiResponse([_call_part("search", {"query": "x"})])

        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.message.tool_calls[0].name == "search"
        assert result.message.tool_calls[0].arguments == {"query": "x"}

    async def test_a_function_call_reports_tool_calls_not_stop(
        self, gemini_provider
    ) -> None:
        """Gemini says STOP even when it asked for a tool; the runner needs the
        distinction to know whether the turn is finished."""
        provider, captured = gemini_provider
        captured["_reply"] = FakeGeminiResponse([_call_part("search", {})])

        result = await provider.complete(ModelRequest(messages=[user("hi")]))
        assert result.finish_reason is FinishReason.TOOL_CALLS

    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("FinishReason.STOP", FinishReason.STOP),
            ("FinishReason.MAX_TOKENS", FinishReason.LENGTH),
            ("FinishReason.SAFETY", FinishReason.CONTENT_FILTER),
            ("MALFORMED_FUNCTION_CALL", FinishReason.ERROR),
            ("SOMETHING_NEW", FinishReason.STOP),
        ],
    )
    async def test_finish_reasons_map(
        self, gemini_provider, reason: str, expected: FinishReason
    ) -> None:
        provider, captured = gemini_provider
        captured["_reply"] = FakeGeminiResponse([_text_part("x")], finish_reason=reason)

        result = await provider.complete(ModelRequest(messages=[user("hi")]))
        assert result.finish_reason is expected

    async def test_mixed_text_and_call_parts(self, gemini_provider) -> None:
        provider, captured = gemini_provider
        captured["_reply"] = FakeGeminiResponse(
            [_text_part("thinking"), _call_part("search", {"query": "y"})]
        )

        result = await provider.complete(ModelRequest(messages=[user("hi")]))

        assert result.message.content == "thinking"
        assert len(result.message.tool_calls) == 1


# ---------------------------------------------------------------------------
# Interchangeability
# ---------------------------------------------------------------------------


class TestProvidersAreInterchangeable:
    def test_all_satisfy_the_protocol(self) -> None:
        from loom.agents.models import ModelProvider
        from loom.agents.providers import (
            AnthropicProvider,
            GeminiProvider,
            OpenAIProvider,
        )

        for cls in (AnthropicProvider, OpenAIProvider, GeminiProvider):
            instance = cls.__new__(cls)
            instance.model_name = "x"
            assert isinstance(instance, ModelProvider), cls.__name__

    def test_the_package_exports_the_three_providers_and_the_env_helpers(self) -> None:
        import importlib

        module = importlib.import_module("loom.agents.providers")
        assert set(module.__all__) == {
            "AnthropicProvider",
            "GeminiProvider",
            "OpenAIProvider",
            # `vendor_of` and the model-selecting form of `from_env` exist so a
            # caller can ask for one *specific* model — a stratified eval run,
            # which is what a suite aimed at small-model compatibility needs.
            "env_keys",
            "from_env",
            "vendor_of",
        }

    def test_from_env_can_select_a_named_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.agents import providers

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert providers.from_env("claude-sonnet-5") is None

    def test_an_unknown_model_family_selects_nothing(self) -> None:
        from loom.agents.providers import vendor_of

        assert vendor_of("llama-3-70b") is None

    def test_importing_the_package_needs_no_vendor_sdk(self) -> None:
        """Lazy exports: the extras are optional, so importing must be free.

        Checked in a subprocess and against `sys.modules`, because that is the
        claim. Enumerating `__all__` does not make it — a module that eagerly
        imported `anthropic` at the top would export exactly the same names and
        pass, which is how this test came to assert a list of strings while the
        property it is named for went unverified.
        """
        import subprocess
        import sys

        # `google.genai`, never bare `google` — that is a *namespace* package
        # shared with `google.protobuf`, which arrives through an unrelated
        # dependency. Matching the top-level name reports a leak on every run.
        probe = (
            "import sys; import loom.agents.providers; "
            "print(sorted(m for m in sys.modules "
            "if m.startswith(('anthropic', 'openai', 'google.genai'))))"
        )
        leaked = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        ).stdout.strip()

        assert leaked == "[]", f"importing the package pulled in {leaked}"

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        import loom.agents.providers as providers

        with pytest.raises(AttributeError, match="CohereProvider"):
            _ = providers.CohereProvider

    async def test_an_agent_runs_against_each_provider(
        self, openai_provider, gemini_provider
    ) -> None:
        """The point of the protocol: swapping vendors is a one-line change."""
        from loom.agents.agent import Agent

        for provider, _ in (openai_provider, gemini_provider):
            agent = Agent(name="portable", model=provider)
            result = await agent("say something")
            assert result.output == "done"


# ---------------------------------------------------------------------------
# Agent results through the journal
# ---------------------------------------------------------------------------


class TestAgentResultsSurviveReplay:
    """Regression tests for two bugs found running a real agent end to end."""

    @staticmethod
    def _scripted_agent():
        from loom.agents.agent import Agent
        from loom.testing import MockModelProvider, mock_response

        return Agent(
            name="scripted",
            model=MockModelProvider(
                responses=[
                    mock_response(
                        "the answer",
                        usage=Usage(requests=1, input_tokens=120, output_tokens=8),
                    )
                ]
            ),
        )

    async def test_agent_result_is_still_typed_on_replay(self) -> None:
        """It decoded back as a plain dict, so `result.output` raised
        AttributeError on the second attempt but not the first."""
        from loom import Context, Runtime, workflow
        from loom.stores.memory import MemoryStore

        agent = self._scripted_agent()

        @workflow(name="agent_replay")
        async def flow(ctx: Context, _input: str) -> str:
            result = await ctx.agent(agent, "question")
            return result.output

        rt = Runtime(store=MemoryStore())
        first = await rt.run(flow, "go")
        replayed = await rt.replay(first.run_id)

        assert first.output == "the answer"
        assert replayed.status.value == "completed"
        assert replayed.output == first.output

    async def test_agent_tokens_reach_the_run_total(self) -> None:
        """total_usage() skipped every agent entry, so an agent run always
        reported zero tokens — the main number anyone wants from it."""
        from loom import Context, Runtime, workflow
        from loom.stores.memory import MemoryStore

        agent = self._scripted_agent()

        @workflow(name="agent_usage")
        async def flow(ctx: Context, _input: str) -> str:
            return (await ctx.agent(agent, "question")).output

        result = await Runtime(store=MemoryStore()).run(flow, "go")

        assert result.usage.input_tokens == 120
        assert result.usage.output_tokens == 8

    def test_a_composite_agent_entry_is_not_double_counted(self) -> None:
        """When per-turn entries exist beneath an agent, its rollup is skipped."""
        from loom.runtime.journal import (
            EntryKind,
            EntryStatus,
            Journal,
            JournalEntry,
        )

        def entry(path: str, kind: EntryKind, tokens: int) -> JournalEntry:
            return JournalEntry(
                path=path,
                kind=kind,
                name=path,
                status=EntryStatus.COMPLETED,
                usage=Usage(input_tokens=tokens),
            )

        journal = Journal(
            [
                entry("0000", EntryKind.AGENT, 100),  # rollup
                entry("0000.0000", EntryKind.STEP, 60),
                entry("0000.0001", EntryKind.STEP, 40),
            ]
        )

        # 60 + 40, not 200.
        assert journal.total_usage().input_tokens == 100


class TestOutputCeilingsAreNotFlat:
    """4096 was a legacy floor, and exceeding it does not fail loudly.

    A model emitting a long tool call spends the ceiling on one argument, never
    closes the JSON, and the provider drops the unterminated block — so the
    response arrives empty and a turn loop reads it as nothing to say. Whole
    authoring jobs died that way, reporting a turn budget that was not the
    cause.
    """

    def test_a_current_model_gets_more_than_the_legacy_floor(self) -> None:
        from loom.agents.providers.anthropic_provider import default_max_tokens

        assert default_max_tokens("claude-sonnet-5") > 4096
        assert default_max_tokens("claude-opus-5") > 4096

    def test_longest_prefix_wins(self) -> None:
        from loom.agents.providers.anthropic_provider import default_max_tokens

        assert default_max_tokens("claude-haiku-4-5-20251001") == 16_000

    def test_an_unknown_model_keeps_the_old_default(self) -> None:
        """Guessing high for a model with no entry gets the request rejected
        outright rather than truncated, which is a worse failure than the one
        this table exists to fix."""
        from loom.agents.providers.anthropic_provider import default_max_tokens

        assert default_max_tokens("some-future-model") == 4096


class TestTheAuthoringCeilingDefersToTheModel:
    """A flat number is right for neither end of the range.

    Too low for a current model and the whole workflow truncates — silently, as
    an empty response, reported as a turn budget. Too high for an older one and
    the request is rejected outright. Only the provider knows which it is.
    """

    def test_a_provider_that_declares_one_wins(self) -> None:
        import os

        os.environ.setdefault("ANTHROPIC_API_KEY", "test")
        from loom.agents.coding_agent import authoring_max_tokens
        from loom.agents.providers.anthropic_provider import NON_STREAMING_LIMIT, AnthropicProvider

        assert authoring_max_tokens(
            AnthropicProvider("claude-sonnet-5")) == NON_STREAMING_LIMIT
        # Its real hard limit — raising this would turn a truncation into a 400.
        assert authoring_max_tokens(AnthropicProvider("claude-3-opus-20240229")) == 8192

    def test_a_provider_that_declares_none_gets_the_floor(self) -> None:
        """A host's own provider, written before any of this, still gets room
        for a source file rather than a chat reply."""
        from loom.agents.coding_agent import AUTHORING_MAX_TOKENS, authoring_max_tokens

        class Bare:
            model_name = "custom"

        assert authoring_max_tokens(Bare()) == AUTHORING_MAX_TOKENS


class TestTheCeilingRespectsTheTransport:
    """A ceiling above what can be *asked for* is not a bigger budget.

    The SDK refuses a non-streaming request whose `max_tokens` implies a
    generation that could run past ten minutes — a ValueError raised before
    anything is sent, so it fails the whole job at turn zero rather than
    degrading. Measured: 21,333 is accepted and 24,000 is not.
    """

    def test_a_large_model_is_clamped(self) -> None:
        from loom.agents.providers.anthropic_provider import (
            MAX_OUTPUT_TOKENS,
            NON_STREAMING_LIMIT,
            default_max_tokens,
        )

        assert MAX_OUTPUT_TOKENS["claude-sonnet"] > NON_STREAMING_LIMIT
        assert default_max_tokens("claude-sonnet-5") == NON_STREAMING_LIMIT

    def test_a_model_below_the_limit_is_untouched(self) -> None:
        """The clamp is a ceiling, not a floor — it must never *raise* a model
        past its own hard limit, which would turn a truncation into a 400."""
        from loom.agents.providers.anthropic_provider import default_max_tokens

        assert default_max_tokens("claude-3-opus-20240229") == 8192
