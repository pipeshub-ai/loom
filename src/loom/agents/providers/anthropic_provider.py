"""Anthropic Claude model provider for the LOOM agent runtime.

Implements the ``ModelProvider`` protocol so any ``Agent`` can use
Claude models without extra configuration — just pass an
``AnthropicProvider`` as ``agent.model``.
"""

from __future__ import annotations

import os
from typing import Any

from loom.agents.messages import (
    Message,
    Role,
    ToolCall,
    assistant,
)
from loom.agents.models import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    ModelSettings,
)
from loom.core.models import Usage

_STOP_REASON_MAP: dict[str, FinishReason] = {
    "end_turn": FinishReason.STOP,
    "tool_use": FinishReason.TOOL_CALLS,
    "max_tokens": FinishReason.LENGTH,
}

#: Models that reject sampling controls outright — a hard 400, not a warning:
#: "`temperature` is deprecated for this model". Claude 4.x still accepts both.
#: The list is deliberately narrow: dropping these silently everywhere would
#: ignore a caller who set them on a model that honours them.
_NO_SAMPLING_CONTROLS = ("claude-sonnet-5", "claude-opus-5", "claude-haiku-5")


def _rejects_sampling_controls(model: str) -> bool:
    """True when ``temperature``/``top_p`` must be omitted for *model*.

    Worth handling here rather than at the call site: a coding agent lowers the
    temperature for determinism, which turns every request into a 400 and takes
    the whole agent down with it.
    """
    return model.startswith(_NO_SAMPLING_CONTROLS)


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
        cache: bool = True,
    ) -> None:
        import anthropic

        self.model_name = model_name
        self._max_tokens = max_tokens
        self._cache = cache
        """Reuse the unchanged prefix of a conversation across turns.

        An agent loop resends its whole context every turn, so the system
        prompt and tool schemas — identical from the first turn to the last —
        are paid for once per turn. Marking them cacheable makes that a read
        against a cache instead. Set ``cache=False`` for a single-shot call,
        where writing a cache costs more than it saves."""
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
            kwargs["system"] = _cacheable_system(system_prompt, self._cache)
        if tools:
            kwargs["tools"] = _cacheable_tools(tools, self._cache)
        if self._cache:
            _mark_message_prefix(messages)
        if not _rejects_sampling_controls(self.model_name):
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


#: Anthropic allows four cache breakpoints per request. Three are spent on the
#: stable prefix — system, tools, and the conversation so far — leaving one
#: spare rather than spreading them thin.
_CACHE_CONTROL = {"type": "ephemeral"}


def _cacheable_system(prompt: str, cache: bool) -> Any:
    """The system prompt, marked cacheable when caching is on."""
    if not cache:
        return prompt
    return [{"type": "text", "text": prompt, "cache_control": _CACHE_CONTROL}]


def _cacheable_tools(tools: list[dict[str, Any]], cache: bool) -> list[dict[str, Any]]:
    """Mark the last tool, which caches every tool definition before it."""
    if not cache or not tools:
        return tools
    marked = [dict(tool) for tool in tools]
    marked[-1]["cache_control"] = _CACHE_CONTROL
    return marked


def _mark_message_prefix(messages: list[dict[str, Any]]) -> None:
    """Cache the conversation up to the last completed exchange.

    An agent loop appends to its history and resends all of it, so everything
    but the newest turn is unchanged from the previous request. Marking the
    second-to-last message makes that prefix a cache read; the newest turn is
    left out because it will not be reused.
    """
    if len(messages) < 3:
        return
    target = messages[-2]
    content = target.get("content")
    if isinstance(content, str):
        target["content"] = [
            {"type": "text", "text": content, "cache_control": _CACHE_CONTROL}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1] = {**content[-1], "cache_control": _CACHE_CONTROL}



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

    # Cache reads are billed at a fraction of the input rate, so reporting them
    # separately is what makes the saving visible rather than assumed.
    usage = Usage(
        cached_input_tokens=int(getattr(raw.usage, "cache_read_input_tokens", 0) or 0),
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
