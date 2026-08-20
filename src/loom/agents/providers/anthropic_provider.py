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


def _empty_text(block: Any) -> bool:
    """An empty text block, which cannot carry ``cache_control``.

    The API rejects that combination outright — *"cache_control cannot be set
    for empty text blocks"*, a 400 rather than a warning. An assistant turn that
    was purely a tool call has exactly this shape, so an agent loop hits it as
    soon as the turn it wants to cache happens to be one. That is late in a long
    conversation, and it fails the whole request: the coding agent lost a
    fifty-turn session to it with no partial result.
    """
    if isinstance(block, str):
        return not block.strip()
    if isinstance(block, dict) and block.get("type", "text") == "text":
        return not str(block.get("text") or "").strip()
    return False


def _mark_message_prefix(messages: list[dict[str, Any]]) -> None:
    """Cache the conversation up to the last completed exchange.

    An agent loop appends to its history and resends all of it, so everything
    but the newest turn is unchanged from the previous request. Marking the
    second-to-last message makes that prefix a cache read; the newest turn is
    left out because it will not be reused.

    A turn with nothing to mark is skipped rather than marked anyway. Losing one
    request's cache read costs tokens; sending the marker on an empty text block
    costs the request.
    """
    if len(messages) < 3:
        return
    target = messages[-2]
    content = target.get("content")
    if isinstance(content, str):
        if _empty_text(content):
            return
        target["content"] = [
            {"type": "text", "text": content, "cache_control": _CACHE_CONTROL}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        if _empty_text(content[-1]):
            return
        content[-1] = {**content[-1], "cache_control": _CACHE_CONTROL}



def _is_tool_result_turn(message: dict[str, Any]) -> bool:
    """Whether *message* is a user turn made only of tool results.

    Only such a turn absorbs another result. A user turn carrying ordinary text
    is somebody's actual message, and appending a tool result to it would
    reorder the conversation.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    )


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
            # Every tool_result answering one assistant turn belongs in a
            # *single* user turn. LOOM's runner appends one TOOL message per
            # call, so a turn with two parallel tool calls produced two
            # consecutive user messages here — a shape the Messages API does
            # not accept, on the path an agent takes constantly. Coalescing
            # into the previous user turn when that turn is itself tool
            # results is what makes parallel tool use work at all.
            block = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id or "",
                "content": msg.content or "",
            }
            if converted and _is_tool_result_turn(converted[-1]):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
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

    # Anthropic reports `input_tokens` *excluding* cache traffic, and reports
    # reads and writes beside it. `Usage.input_tokens` is defined as the total,
    # the way OpenAI's `prompt_tokens` already is, so the three are added here
    # rather than left for the cost model to reconcile — which it could not,
    # having no way to tell which vendor answered. Subtracting the reads from a
    # count that already excluded them billed real input tokens at zero.
    read = int(getattr(raw.usage, "cache_read_input_tokens", 0) or 0)
    written = int(getattr(raw.usage, "cache_creation_input_tokens", 0) or 0)
    usage = Usage(
        requests=1,
        input_tokens=int(raw.usage.input_tokens or 0) + read + written,
        cached_input_tokens=read,
        cache_write_tokens=written,
        output_tokens=int(raw.usage.output_tokens or 0),
    )

    finish_reason = _STOP_REASON_MAP.get(raw.stop_reason or "", FinishReason.STOP)

    return ModelResponse(
        message=message,
        usage=usage,
        finish_reason=finish_reason,
        model=raw.model,
        raw={"stop_reason": raw.stop_reason},
    )
