"""OpenAI model provider for the LOOM agent runtime.

Implements the ``ModelProvider`` protocol, so any ``Agent`` can use GPT models
by passing an ``OpenAIProvider`` as ``agent.model`` — nothing else changes.

Also works against any OpenAI-compatible endpoint (Azure OpenAI, Together,
Groq, vLLM, Ollama) via ``base_url``, since that wire format has become the
de-facto interchange standard.

    pip install loomflow[openai]
"""

from __future__ import annotations

import json
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

_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}

#: Reasoning models reject ``temperature``/``top_p`` and want
#: ``max_completion_tokens`` rather than ``max_tokens``. Matched by prefix
#: because the family keeps growing.
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

#: Models that reject function tools on chat/completions unless
#: ``reasoning_effort`` is explicitly ``"none"``:
#:
#:     Function tools with reasoning_effort are not supported for
#:     gpt-5.6-luna in /v1/chat/completions.
#:
#: Verified against the live API: the whole gpt-5.6 generation needs it, while
#: gpt-5.4/5.5 work either way and gpt-5 and gpt-4.1 *reject* ``"none"``. So this
#: is deliberately a narrow list rather than a family-wide prefix — applying it
#: broadly breaks the models that do not want it.
_TOOLS_NEED_EFFORT_NONE = ("gpt-5.6",)


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(_REASONING_PREFIXES)


def _tools_need_effort_none(model: str) -> bool:
    return model.startswith(_TOOLS_NEED_EFFORT_NONE)


class OpenAIProvider:
    """Wraps the ``openai`` SDK as a LOOM ``ModelProvider``.

    Parameters
    ----------
    model_name:
        Any chat-completions model ID, e.g. ``"gpt-5.6-luna"`` or ``"gpt-4.1"``.
    api_key:
        Falls back to ``OPENAI_API_KEY``.
    base_url:
        Point at an OpenAI-compatible server. Falls back to ``OPENAI_BASE_URL``.
    organization, project:
        Optional OpenAI account scoping.
    max_tokens:
        Default completion ceiling, overridable per request.
    """

    def __init__(
        self,
        model_name: str = "gpt-5.6-luna",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        import openai

        self.model_name = model_name
        self._max_tokens = max_tokens
        self._client = openai.AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL") or None,
            organization=organization,
            project=project,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send *request* to OpenAI and return a normalised response."""
        model = request.model or self.model_name
        settings: ModelSettings = request.settings

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(request.messages),
        }

        limit = settings.max_tokens or self._max_tokens
        if _is_reasoning_model(model):
            # Reasoning models bill thinking tokens against a different ceiling
            # and reject the sampling knobs outright.
            kwargs["max_completion_tokens"] = limit
            if settings.reasoning_effort:
                kwargs["reasoning_effort"] = settings.reasoning_effort
        else:
            kwargs["max_tokens"] = limit
            if settings.temperature is not None:
                kwargs["temperature"] = settings.temperature
            if settings.top_p is not None:
                kwargs["top_p"] = settings.top_p

        if request.tools:
            kwargs["tools"] = _build_tools(request)
            if settings.tool_choice:
                kwargs["tool_choice"] = _tool_choice(settings.tool_choice)
            if settings.parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = settings.parallel_tool_calls
            if _tools_need_effort_none(model) and not settings.reasoning_effort:
                # Without this the call is a hard 400, which would make the
                # model unusable for agent work — tools are the normal case
                # here. An explicit reasoning_effort from the caller still wins,
                # so the override is a default rather than a policy.
                kwargs["reasoning_effort"] = "none"
        if request.output_schema:
            kwargs["response_format"] = _response_format(request.output_schema)
        if settings.stop:
            kwargs["stop"] = settings.stop
        if settings.seed is not None:
            kwargs["seed"] = settings.seed
        if settings.timeout is not None:
            kwargs["timeout"] = settings.timeout
        kwargs.update(settings.extra)

        raw = await self._client.chat.completions.create(**kwargs)
        return _parse_response(raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert LOOM messages to the chat-completions format."""
    converted: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role is Role.TOOL:
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "",
                    "content": msg.content or "",
                }
            )
            continue

        if msg.role is Role.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            # OpenAI carries arguments as a JSON *string*.
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in msg.tool_calls
                ]
                # The API rejects assistant turns with neither content nor calls,
                # and null content alongside tool calls is the documented shape.
                entry["content"] = msg.content or None
            converted.append(entry)
            continue

        converted.append({"role": msg.role.value, "content": msg.content or ""})

    return converted


def _build_tools(request: ModelRequest) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool in request.tools:
        parameters = dict(tool.parameters) if tool.parameters else {}
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        function: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        }
        if tool.strict:
            # Strict mode requires every property to be required and additional
            # properties disallowed; enforce it rather than letting the API 400.
            function["strict"] = True
            function["parameters"] = _strictify(parameters)
        tools.append({"type": "function", "function": function})
    return tools


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON Schema satisfy OpenAI's strict-mode rules."""
    tightened = dict(schema)
    properties = tightened.get("properties") or {}
    tightened["additionalProperties"] = False
    tightened["required"] = list(properties)
    return tightened


def _tool_choice(choice: str) -> Any:
    """Map the neutral tool_choice to OpenAI's shape."""
    if choice in ("auto", "none", "required"):
        return choice
    return {"type": "function", "function": {"name": choice}}


def _response_format(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.get("title", "output"),
            "schema": schema,
            "strict": False,
        },
    }


def _parse_response(raw: Any) -> ModelResponse:
    """Convert an OpenAI ``ChatCompletion`` into a LOOM ``ModelResponse``."""
    choice = raw.choices[0] if raw.choices else None
    payload = choice.message if choice else None

    tool_calls: list[ToolCall] = []
    for call in getattr(payload, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if function is None:
            continue
        tool_calls.append(
            ToolCall(
                id=call.id,
                name=function.name,
                arguments=_decode_arguments(function.arguments),
            )
        )

    message = assistant(
        content=getattr(payload, "content", None) or None,
        tool_calls=tool_calls,
    )

    raw_usage = getattr(raw, "usage", None)
    usage = Usage()
    if raw_usage is not None:
        usage.requests = 1
        usage.input_tokens = getattr(raw_usage, "prompt_tokens", 0) or 0
        usage.output_tokens = getattr(raw_usage, "completion_tokens", 0) or 0
        details = getattr(raw_usage, "prompt_tokens_details", None)
        usage.cached_input_tokens = getattr(details, "cached_tokens", 0) or 0
        completion_details = getattr(raw_usage, "completion_tokens_details", None)
        usage.reasoning_tokens = getattr(completion_details, "reasoning_tokens", 0) or 0

    reason = getattr(choice, "finish_reason", None) or ""
    return ModelResponse(
        message=message,
        usage=usage,
        finish_reason=_FINISH_REASON_MAP.get(reason, FinishReason.STOP),
        model=getattr(raw, "model", "") or "",
        raw={"finish_reason": reason},
    )


def _decode_arguments(arguments: Any) -> dict[str, Any]:
    """Parse a tool call's JSON arguments.

    A model can emit malformed JSON. Returning the raw text under ``_raw`` beats
    raising, because the runner surfaces it to the model as a tool error and the
    next turn usually fixes it.
    """
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        decoded = json.loads(arguments)
    except (TypeError, ValueError):
        return {"_raw": str(arguments)}
    return decoded if isinstance(decoded, dict) else {"_raw": decoded}
