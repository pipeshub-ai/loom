"""Model provider abstraction.

The SDK never depends on a specific vendor SDK. A provider implements one method,
:meth:`ModelProvider.complete`, and everything else — durability, retries, structured
output, guardrails, cost accounting — is handled by the runner around it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from loom.agents.messages import Message
from loom.core.models import Usage


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class ToolSchema(BaseModel):
    """A tool as advertised to the model."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: bool = False


class ModelSettings(BaseModel):
    """Provider-neutral generation settings.

    Resolved in layers — provider default, then agent, then run — so a single run can dial
    temperature down without redefining the agent.
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    seed: int | None = None
    parallel_tool_calls: bool | None = None
    tool_choice: str | None = None
    """``"auto"``, ``"required"``, ``"none"``, or a specific tool name."""
    timeout: float | None = None
    reasoning_effort: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def merged_with(self, other: ModelSettings | None) -> ModelSettings:
        """Overlay ``other`` on top of this, ignoring unset fields."""
        if other is None:
            return self
        overrides = other.model_dump(exclude_unset=True, exclude_none=True)
        extra = {**self.extra, **overrides.pop("extra", {})}
        return self.model_copy(update={**overrides, "extra": extra})


class ModelRequest(BaseModel):
    """Everything a provider needs for one completion."""

    messages: list[Message]
    tools: list[ToolSchema] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    """Set when the agent asked for native structured output."""
    settings: ModelSettings = Field(default_factory=ModelSettings)
    model: str | None = None


class ModelResponse(BaseModel):
    """One completion, plus what it cost."""

    message: Message
    usage: Usage = Field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.STOP
    model: str = ""
    raw: dict[str, Any] | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """The single integration point for any LLM vendor."""

    model_name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


#: USD per million tokens, as ``(input, output)``. Advisory only — override per
#: provider. Lookup is exact first, then longest-prefix, so a dated model id
#: like ``gpt-4.1-2025-04-14`` resolves to its family.
PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5.6-luna": (0.20, 1.80),
    "o1": (15.00, 60.00),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # Anthropic
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-haiku-3.5": (0.80, 4.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # Google
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost(model: str, usage: Usage) -> float:
    """Best-effort USD cost. Returns ``0.0`` for models with no price on file.

    Cost is a first-class metric here rather than an afterthought, because a budget you
    cannot measure is a budget you cannot enforce.
    """
    prices = PRICING.get(model)
    if prices is None:
        # Longest prefix wins. Taking the first match in dict order would price
        # "gpt-4.1-mini-2025-04-14" as "gpt-4.1" — five times too much — because
        # the family entry is also a prefix of the mini one.
        candidates = [key for key in PRICING if model.startswith(key)]
        if not candidates:
            return 0.0
        prices = PRICING[max(candidates, key=len)]
    input_price, output_price = prices
    billable_input = max(0, usage.input_tokens - usage.cached_input_tokens)
    cached = usage.cached_input_tokens * input_price * 0.25
    return (
        billable_input * input_price + usage.output_tokens * output_price + cached
    ) / 1_000_000
