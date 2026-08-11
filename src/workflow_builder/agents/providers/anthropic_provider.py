"""Anthropic Claude model provider for the LOOM agent runtime.

Implements the ``ModelProvider`` protocol so any ``Agent`` can use
Claude models without extra configuration — just pass an
``AnthropicProvider`` as ``agent.model``.
"""

from __future__ import annotations

import os
from typing import Any

from workflow_builder.agents.messages import (
    Message,
    Role,
    ToolCall,
    assistant,
)
from workflow_builder.agents.models import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    ModelSettings,
)
from workflow_builder.core.models import Usage

_STOP_REASON_MAP: dict[str, FinishReason] = {
    "end_turn": FinishReason.STOP,
    "tool_use": FinishReason.TOOL_CALLS,
    "max_tokens": FinishReason.LENGTH,
}


class AnthropicProvider:
    """Wraps the ``anthropic`` SDK as a LOOM ``ModelProvider``.

    Parameters
    ----------
    model_name:
        Any Anthropic model ID, e.g. ``"claude-sonnet-5"``.
    api_key:
        API key. Falls back to ``ANTHROPIC_API_KEY`` env var.
    max_tokens:
        Default maximum tokens for a completion (overridable per request).
    """

    def __init__(
        self,
        model_name: str = "claude-sonnet-5",
        *,
        api_key: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        import anthropic

        self.model_name = model_name
        self._max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send *request* to Claude and return a normalised response."""
        messages, system_prompt = _split_messages(request.messages)
        tools = _build_tools(request) if request.tools else []

        settings: ModelSettings = request.settings
        max_tokens = settings.max_tokens or self._max_tokens

        kwargs: dict[str, Any] = {
            "model": request.model or self.model_name,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = tools
        if settings.temperature is not None:
            kwargs["temperature"] = settings.temperature
        if settings.top_p is not None:
            kwargs["top_p"] = settings.top_p
        if settings.stop:
            kwargs["stop_sequences"] = settings.stop

        raw = await self._client.messages.create(**kwargs)
        return _parse_response(raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_messages(
    messages: list[Message],
) -> tuple[list[dict[str, Any]], str]:
    """Separate the system prompt from the rest and convert to Anthropic format."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == Role.SYSTEM:
            if msg.content:
                system_parts.append(msg.content)
            continue

        if msg.role == Role.TOOL:
            # Tool result — attach to preceding assistant turn or start new user turn
            converted.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": msg.content or "",
                    }
                ],
            })
            continue

        if msg.role == Role.ASSISTANT:
            content: list[dict[str, Any]] = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                })
            converted.append({"role": "assistant", "content": content or msg.content or ""})
            continue

        # USER
        converted.append({"role": "user", "content": msg.content or ""})

    return converted, "\n\n".join(system_parts)


def _build_tools(request: ModelRequest) -> list[dict[str, Any]]:
    tools = []
    for t in request.tools:
        schema = dict(t.parameters) if t.parameters else {}
        # Anthropic requires "type": "object" on every input_schema
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        tools.append({
            "name": t.name,
            "description": t.description,
            "input_schema": schema,
        })
    return tools


def _parse_response(raw: Any) -> ModelResponse:
    """Convert an Anthropic ``Message`` to a LOOM ``ModelResponse``."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in raw.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or {},
                )
            )

    message = assistant(
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls,
    )

    usage = Usage(
        input_tokens=raw.usage.input_tokens,
        output_tokens=raw.usage.output_tokens,
    )

    finish_reason = _STOP_REASON_MAP.get(raw.stop_reason or "", FinishReason.STOP)

    return ModelResponse(
        message=message,
        usage=usage,
        finish_reason=finish_reason,
        model=raw.model,
        raw={"stop_reason": raw.stop_reason},
    )
