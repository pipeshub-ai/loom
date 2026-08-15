"""Model capability detection and tiering.

Infers a model's capability tier from its identifier so the system
can select the right prompt compression, schema simplification,
and scaffolding level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ModelTier(StrEnum):
    """Capability tier derived from model size / family."""

    LARGE = "large"  # >=70B or frontier models
    MEDIUM = "medium"  # 14B-70B
    SMALL = "small"  # <14B


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Feature profile for a given model tier."""

    tier: ModelTier
    supports_tool_use: bool = True
    supports_structured_output: bool = False
    supports_parallel_tools: bool = False
    max_reliable_output_tokens: int = 2048
    json_mode_available: bool = False


# Each entry is (compiled_regex_pattern, tier).  First match wins.
_MODEL_PATTERNS: list[tuple[str, ModelTier]] = [
    # --- LARGE (frontier / >=70B) ---
    (r"gpt-4", ModelTier.LARGE),
    (r"claude-3\.5|claude-sonnet|claude-opus|claude-4", ModelTier.LARGE),
    (r"gemini.*pro|gemini.*ultra", ModelTier.LARGE),
    (r"o1|o3|o4", ModelTier.LARGE),
    # --- MEDIUM (14B-70B) ---
    (r"llama.*70b|qwen.*72b|mixtral", ModelTier.MEDIUM),
    (r"claude-haiku|gemini.*flash", ModelTier.MEDIUM),
    (r"gpt-3\.5", ModelTier.MEDIUM),
    # --- SMALL (<14B) ---
    (
        r"llama.*8b|mistral.*7b|phi-3|gemma.*9b|qwen.*7b",
        ModelTier.SMALL,
    ),
    (r"llama.*3b|phi-2|gemma.*2b", ModelTier.SMALL),
]

_COMPILED_PATTERNS: list[tuple[re.Pattern[str], ModelTier]] = [
    (re.compile(pat), tier) for pat, tier in _MODEL_PATTERNS
]


def detect_tier(model_id: str) -> ModelTier:
    """Return the capability tier for *model_id*.

    The identifier is lower-cased and matched against known regex
    patterns.  If nothing matches the model is assumed ``MEDIUM``.
    """
    model_lower = model_id.lower()
    for pattern, tier in _COMPILED_PATTERNS:
        if pattern.search(model_lower):
            return tier
    return ModelTier.MEDIUM


def detect_capabilities(model_id: str) -> ModelCapabilities:
    """Build a full capability profile for *model_id*."""
    tier = detect_tier(model_id)

    if tier is ModelTier.LARGE:
        return ModelCapabilities(
            tier=tier,
            supports_tool_use=True,
            supports_structured_output=True,
            supports_parallel_tools=True,
            max_reliable_output_tokens=4096,
            json_mode_available=True,
        )

    if tier is ModelTier.SMALL:
        return ModelCapabilities(
            tier=tier,
            supports_tool_use=True,
            supports_structured_output=False,
            supports_parallel_tools=False,
            max_reliable_output_tokens=1024,
            json_mode_available=False,
        )

    # MEDIUM (default)
    return ModelCapabilities(
        tier=tier,
        supports_tool_use=True,
        supports_structured_output=True,
        supports_parallel_tools=False,
        max_reliable_output_tokens=2048,
        json_mode_available=True,
    )
