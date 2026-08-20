"""Model provider abstraction.

The SDK never depends on a specific vendor SDK. A provider implements one method,
:meth:`ModelProvider.complete`, and everything else — durability, retries, structured
output, guardrails, cost accounting — is handled by the runner around it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NamedTuple, Protocol, TypeVar, runtime_checkable

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


def supports_native_output(provider: Any) -> bool:
    """Whether *provider* can be asked for a schema-conforming JSON response.

    Read from the provider rather than hard-coded. The runner passed
    ``supports_native=False`` literally, so ``OutputMode.NATIVE`` was
    unreachable and ``_response_format`` in the OpenAI provider — which exists
    and is correct — was dead code; every structured output went through a
    synthetic ``final_output`` tool instead.

    Absent attribute means ``False``, so a provider written before this, or one
    a host supplies, is unaffected. Shipped providers leave it off: turning it
    on changes what the model returns, and that is a decision to make against a
    live API rather than in a refactor.
    """
    return bool(getattr(provider, "supports_native_output", False))


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
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.80),
    "o1": (15.00, 60.00),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # Anthropic
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-5": (1.00, 5.00),
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


class CacheRates(NamedTuple):
    """What a cached prompt token costs, as a multiple of the input rate.

    Its own table rather than two more columns on :data:`PRICING`, because it
    is a different kind of fact: prices are per model and change per release,
    while these are a vendor's *policy* and hold across a whole family. Both
    are resolved by the same longest-prefix rule.
    """

    read: float
    """Multiplier for a token served from cache."""
    write: float
    """Multiplier for a token written into cache — above 1.0, always."""


#: Per-family cache economics. The flat ``0.25`` this replaced was right for no
#: vendor: Anthropic reads at a tenth and charges a quarter *extra* to write,
#: OpenAI reads at a half and charges nothing to write. Cache writes were not
#: counted at all, which under-billed every agent loop — a loop writes a cache
#: entry on almost every turn.
CACHE_RATES: dict[str, CacheRates] = {
    "claude": CacheRates(read=0.10, write=1.25),
    "gpt": CacheRates(read=0.50, write=1.00),
    "o1": CacheRates(read=0.50, write=1.00),
    "o3": CacheRates(read=0.50, write=1.00),
    "o4": CacheRates(read=0.50, write=1.00),
    "gemini": CacheRates(read=0.25, write=1.00),
}

DEFAULT_CACHE_RATES = CacheRates(read=1.0, write=1.0)
"""No discount and no surcharge — the safe assumption for a vendor whose policy
is not on file. Erring towards *more* expensive keeps a budget honest."""


_T = TypeVar("_T")


def _longest_prefix(table: dict[str, _T], model: str) -> _T | None:
    """Exact match, then the longest key that prefixes *model*.

    Taking the first match in dict order would price
    ``gpt-4.1-mini-2025-04-14`` as ``gpt-4.1`` — five times too much — because
    the family entry is also a prefix of the mini one.
    """
    if model in table:
        return table[model]
    candidates = [key for key in table if model.startswith(key)]
    if not candidates:
        return None
    return table[max(candidates, key=len)]


def is_priced(model: str) -> bool:
    """Whether a dollar figure for *model* is anything but a guess.

    Exposed because ``estimate_cost`` returning ``0.0`` for an unknown model
    silently disables every ``max_cost_usd`` budget downstream — a ceiling that
    can never be reached is not a ceiling. A caller that enforces a budget
    should refuse to start rather than believe a zero.
    """
    return _longest_prefix(PRICING, model) is not None


def estimate_cost(model: str, usage: Usage) -> float:
    """Best-effort USD cost. Returns ``0.0`` for models with no price on file.

    Cost is a first-class metric here rather than an afterthought, because a
    budget you cannot measure is a budget you cannot enforce — which is why the
    arithmetic below is stated against one token convention
    (:class:`~loom.core.models.Usage`) that every provider normalises to. It
    used to subtract cache reads from an input count that, for Anthropic,
    already excluded them: 500 real input tokens beside 20,000 cache reads were
    billed as zero.
    """
    prices = _longest_prefix(PRICING, model)
    if prices is None:
        return 0.0
    input_price, output_price = prices
    rates = _longest_prefix(CACHE_RATES, model) or DEFAULT_CACHE_RATES

    cached = usage.cached_input_tokens
    written = usage.cache_write_tokens
    if cached + written > usage.input_tokens:
        # The object is not in the shape `Usage` documents — its total is
        # smaller than its parts. That is the *old* convention, where
        # `input_tokens` excluded cache traffic, so it is what a stored
        # `Usage` from before normalisation looks like, and what third-party
        # code written against Anthropic's own field names produces.
        #
        # Read as fresh-plus-cache rather than clamped. Clamping is the
        # tempting reading and it fails *open*: it would drop the fresh
        # tokens to zero and undercount, which is the wrong direction for a
        # number that backs `max_cost_usd`.
        fresh = usage.input_tokens
    else:
        fresh = usage.input_tokens - cached - written

    return (
        fresh * input_price
        + cached * input_price * rates.read
        + written * input_price * rates.write
        + usage.output_tokens * output_price
    ) / 1_000_000
