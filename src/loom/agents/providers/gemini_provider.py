"""Google Gemini model provider for the LOOM agent runtime.

Implements the ``ModelProvider`` protocol, so any ``Agent`` can use Gemini by
passing a ``GeminiProvider`` as ``agent.model``.

    pip install loomsdk[gemini]

Gemini's wire format diverges from the OpenAI-shaped one more than most:

* turns are ``contents`` with ``parts``, and the assistant role is ``model``
* the system prompt is configuration, not a message
* a function *response* is keyed by the function's **name**, not by a call id

That last one is the sharp edge. LOOM tracks tool calls by id, so this module
maps ids back to names using the assistant turn that requested them.
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
    "STOP": FinishReason.STOP,
    "MAX_TOKENS": FinishReason.LENGTH,
    "SAFETY": FinishReason.CONTENT_FILTER,
    "RECITATION": FinishReason.CONTENT_FILTER,
    "PROHIBITED_CONTENT": FinishReason.CONTENT_FILTER,
    "BLOCKLIST": FinishReason.CONTENT_FILTER,
    "MALFORMED_FUNCTION_CALL": FinishReason.ERROR,
}

#: JSON Schema keywords Gemini's function declarations reject.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$defs",
        "definitions",
        "$ref",
        "allOf",
        "oneOf",
        "not",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "const",
    }
)


class GeminiProvider:
    """Wraps the ``google-genai`` SDK as a LOOM ``ModelProvider``.

    Parameters
    ----------
    model_name:
        Any Gemini model ID, e.g. ``"gemini-2.5-pro"``.
    api_key:
        Falls back to ``GEMINI_API_KEY``, then ``GOOGLE_API_KEY``.
    max_tokens:
        Default output ceiling, overridable per request.
    client:
        A pre-built ``genai.Client``, for Vertex AI or custom transport.
    """

    def __init__(
        self,
        model_name: str = "gemini-2.5-pro",
        *,
        api_key: str | None = None,
        max_tokens: int = 16384,
        client: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self._max_tokens = max_tokens
        # Deliberately untyped: the SDK's overloads insist on its own TypedDicts,
        # while the wire format accepts the plain dicts built below.
        self._client: Any

        if client is not None:
            self._client = client
            return

        from google import genai

        self._client = genai.Client(
            api_key=api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY", "")
        )

    @property
    def max_tokens(self) -> int:
        """This provider's default output ceiling.

        Public so a caller whose deliverable is large — the coding agent, whose
        answer is a whole source file — can defer to the model's real limit
        instead of imposing a flat number that is too low for one model and
        rejected outright by another.
        """
        return self._max_tokens

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send *request* to Gemini and return a normalised response."""
        contents, system_prompt = _to_contents(request.messages)
        settings: ModelSettings = request.settings

        config: dict[str, Any] = {
            "max_output_tokens": settings.max_tokens or self._max_tokens,
        }
        if system_prompt:
            config["system_instruction"] = system_prompt
        if settings.temperature is not None:
            config["temperature"] = settings.temperature
        if settings.top_p is not None:
            config["top_p"] = settings.top_p
        if settings.stop:
            config["stop_sequences"] = settings.stop
        if settings.seed is not None:
            config["seed"] = settings.seed
        if request.tools:
            config["tools"] = [{"function_declarations": _build_tools(request)}]
        if request.output_schema:
            # Gemini takes the schema directly rather than wrapping it.
            config["response_mime_type"] = "application/json"
            config["response_schema"] = _clean_schema(request.output_schema)
        config.update(settings.extra)

        raw = await self._client.aio.models.generate_content(
            model=request.model or self.model_name,
            contents=contents,
            config=config,
        )
        return _parse_response(raw, self.model_name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_contents(
    messages: list[Message],
) -> tuple[list[dict[str, Any]], str]:
    """Convert LOOM messages into Gemini ``contents`` plus a system instruction.

    Tool results carry a call id in LOOM but must carry the function *name* for
    Gemini, so calls seen on assistant turns are indexed by id as we go. Falling
    back to the message's own ``name`` covers a transcript that was rehydrated
    without its originating turn.
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}

    for msg in messages:
        if msg.role is Role.SYSTEM:
            if msg.content:
                system_parts.append(msg.content)
            continue

        if msg.role is Role.TOOL:
            name = call_names.get(msg.tool_call_id or "") or msg.name or "tool"
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": name,
                                "response": _as_response_payload(msg.content),
                            }
                        }
                    ],
                }
            )
            continue

        if msg.role is Role.ASSISTANT:
            parts: list[dict[str, Any]] = []
            if msg.content:
                parts.append({"text": msg.content})
            for call in msg.tool_calls:
                call_names[call.id] = call.name
                parts.append(
                    {"function_call": {"name": call.name, "args": call.arguments}}
                )
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        contents.append({"role": "user", "parts": [{"text": msg.content or ""}]})

    return contents, "\n\n".join(system_parts)


def _as_response_payload(content: str | None) -> dict[str, Any]:
    """Gemini wants a function response as an object, not a bare string."""
    if not content:
        return {"result": ""}
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        return {"result": content}
    return decoded if isinstance(decoded, dict) else {"result": decoded}


def _build_tools(request: ModelRequest) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in request.tools:
        parameters = _clean_schema(dict(tool.parameters) if tool.parameters else {})
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        declaration: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
        }
        # A declaration with an empty parameter object is rejected; omit it.
        if parameters.get("properties"):
            declaration["parameters"] = parameters
        declarations.append(declaration)
    return declarations


def _clean_schema(schema: Any) -> Any:
    """Strip JSON Schema keywords Gemini's validator rejects.

    Pydantic emits ``$defs``/``$ref`` for nested models and
    ``additionalProperties`` everywhere; passing those through is a 400 rather
    than a graceful degradation, so they are removed recursively.
    """
    if isinstance(schema, list):
        return [_clean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned = {
        key: _clean_schema(value)
        for key, value in schema.items()
        if key not in _UNSUPPORTED_SCHEMA_KEYS
    }
    # anyOf survives, but Gemini wants it as the only type discriminator.
    if "anyOf" in cleaned and "type" in cleaned:
        cleaned.pop("type")
    return cleaned


def _parse_response(raw: Any, fallback_model: str) -> ModelResponse:
    """Convert a Gemini ``GenerateContentResponse`` to a LOOM ``ModelResponse``."""
    candidates = getattr(raw, "candidates", None) or []
    candidate = candidates[0] if candidates else None

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    content = getattr(candidate, "content", None)
    for part in getattr(content, "parts", None) or []:
        text = getattr(part, "text", None)
        if text:
            text_parts.append(text)
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None):
            tool_calls.append(
                ToolCall(name=call.name, arguments=dict(getattr(call, "args", None) or {}))
            )

    message = assistant(
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls,
    )

    metadata = getattr(raw, "usage_metadata", None)
    usage = Usage()
    if metadata is not None:
        usage.requests = 1
        # `prompt_token_count` already includes cached content, which is the
        # convention `Usage.input_tokens` declares, so nothing is added here.
        # Anthropic is the odd one out and normalises in its own provider.
        usage.input_tokens = getattr(metadata, "prompt_token_count", 0) or 0
        usage.output_tokens = getattr(metadata, "candidates_token_count", 0) or 0
        usage.cached_input_tokens = getattr(metadata, "cached_content_token_count", 0) or 0
        usage.reasoning_tokens = getattr(metadata, "thoughts_token_count", 0) or 0

    reason = str(getattr(candidate, "finish_reason", "") or "")
    # The SDK returns an enum whose str() is like "FinishReason.STOP".
    reason = reason.rsplit(".", 1)[-1]

    # A function call ends the turn without Gemini labelling it as such.
    finish = _FINISH_REASON_MAP.get(reason, FinishReason.STOP)
    if tool_calls and finish is FinishReason.STOP:
        finish = FinishReason.TOOL_CALLS

    return ModelResponse(
        message=message,
        usage=usage,
        finish_reason=finish,
        model=getattr(raw, "model_version", None) or fallback_model,
        raw={"finish_reason": reason},
    )
