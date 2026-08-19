"""Model provider quirks.

Each vendor rejects some combination the others accept, and each rejection is a
hard 400 rather than a warning. These tests pin the narrow rules that absorb
them, because the failure mode is an agent that cannot make a single call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestAnthropicPromptCaching:
    """`cache_control` on an empty text block is a hard 400.

    "cache_control cannot be set for empty text blocks" — and the block that is
    empty is an assistant turn that was purely a tool call, which an agent loop
    produces constantly. The marker lands on whichever turn happens to be second
    from the end, so this fires deep into a long conversation and takes the
    whole request with it: the coding agent lost a fifty-turn session to it and
    returned no code at all.
    """

    def test_an_empty_text_block_is_not_marked(self) -> None:
        from loom.agents.providers.anthropic_provider import _mark_message_prefix

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
            {"role": "user", "content": "again"},
        ]

        _mark_message_prefix(messages)

        assert "cache_control" not in messages[1]["content"][0]

    def test_an_empty_string_turn_is_not_marked(self) -> None:
        from loom.agents.providers.anthropic_provider import _mark_message_prefix

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": "again"},
        ]

        _mark_message_prefix(messages)

        assert messages[1]["content"] == "   "

    def test_a_tool_use_block_is_still_marked(self) -> None:
        """Only *empty text* is refused. A tool_use block caches fine, and it is
        the common tail of an assistant turn — skipping those would give up the
        cache read on most of an agent loop."""
        from loom.agents.providers.anthropic_provider import _mark_message_prefix

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
            },
            {"role": "user", "content": "again"},
        ]

        _mark_message_prefix(messages)

        assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_a_real_turn_is_marked(self) -> None:
        from loom.agents.providers.anthropic_provider import _mark_message_prefix

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a real answer"},
            {"role": "user", "content": "again"},
        ]

        _mark_message_prefix(messages)

        assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert messages[1]["content"][0]["text"] == "a real answer"


class TestAnthropicSamplingControls:
    """Claude 5 rejects temperature and top_p with a hard 400.

    A coding agent lowers the temperature for determinism, so sending it turns
    every request into an error and takes the whole agent down.
    """

    def test_claude_5_is_detected(self) -> None:
        from loom.agents.providers.anthropic_provider import (
            _rejects_sampling_controls,
        )

        for model in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-5"):
            assert _rejects_sampling_controls(model), model

    def test_claude_4_still_accepts_them(self) -> None:
        """Narrow on purpose: dropping these everywhere ignores the caller."""
        from loom.agents.providers.anthropic_provider import (
            _rejects_sampling_controls,
        )

        for model in ("claude-sonnet-4-6", "claude-3-5-sonnet-20241022"):
            assert not _rejects_sampling_controls(model), model

    async def test_the_request_omits_them_for_claude_5(self) -> None:
        pytest.importorskip("anthropic")
        from loom.agents.models import ModelRequest, ModelSettings
        from loom.agents.providers import AnthropicProvider

        sent: dict = {}

        class FakeMessages:
            async def create(self, **kwargs):
                sent.update(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="ok")],
                    stop_reason="end_turn",
                    model=kwargs.get("model", ""),
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

        provider = AnthropicProvider("claude-sonnet-5", api_key="x")
        provider._client = SimpleNamespace(messages=FakeMessages())

        await provider.complete(
            ModelRequest(
                messages=[{"role": "user", "content": "hi"}],
                settings=ModelSettings(temperature=0.2, top_p=0.9, max_tokens=16),
            )
        )

        assert "temperature" not in sent
        assert "top_p" not in sent

    async def test_the_request_keeps_them_for_claude_4(self) -> None:
        pytest.importorskip("anthropic")
        from loom.agents.models import ModelRequest, ModelSettings
        from loom.agents.providers import AnthropicProvider

        sent: dict = {}

        class FakeMessages:
            async def create(self, **kwargs):
                sent.update(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="ok")],
                    stop_reason="end_turn",
                    model=kwargs.get("model", ""),
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

        provider = AnthropicProvider("claude-sonnet-4-6", api_key="x")
        provider._client = SimpleNamespace(messages=FakeMessages())

        await provider.complete(
            ModelRequest(
                messages=[{"role": "user", "content": "hi"}],
                settings=ModelSettings(temperature=0.2, top_p=0.9, max_tokens=16),
            )
        )

        assert sent["temperature"] == 0.2
        assert sent["top_p"] == 0.9
