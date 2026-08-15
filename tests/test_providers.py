"""Model provider quirks.

Each vendor rejects some combination the others accept, and each rejection is a
hard 400 rather than a warning. These tests pin the narrow rules that absorb
them, because the failure mode is an agent that cannot make a single call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


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
