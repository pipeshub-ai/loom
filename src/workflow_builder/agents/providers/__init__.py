"""Model providers.

Each wraps one vendor SDK behind the :class:`ModelProvider` protocol, which is a
single ``complete()`` method. Everything else — durability, retries, structured
output, guardrails, cost accounting — is the runner's job, so swapping vendors
is a one-line change at the ``Agent``.

Imports are lazy: the vendor SDKs are optional extras, and importing this
package must not require any of them.

    pip install workflow-builder[anthropic]   # AnthropicProvider
    pip install workflow-builder[openai]      # OpenAIProvider
    pip install workflow-builder[gemini]      # GeminiProvider
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_builder.agents.providers.anthropic_provider import AnthropicProvider
    from workflow_builder.agents.providers.gemini_provider import GeminiProvider
    from workflow_builder.agents.providers.openai_provider import OpenAIProvider

__all__ = ["AnthropicProvider", "GeminiProvider", "OpenAIProvider"]

_MODULES = {
    "AnthropicProvider": "anthropic_provider",
    "OpenAIProvider": "openai_provider",
    "GeminiProvider": "gemini_provider",
}


def __getattr__(name: str) -> Any:
    """Import a provider on first use, so a missing SDK costs nothing here."""
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    return getattr(
        import_module(f"workflow_builder.agents.providers.{module}"), name
    )
