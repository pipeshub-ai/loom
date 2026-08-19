"""Model providers.

Each wraps one vendor SDK behind the :class:`ModelProvider` protocol, which is a
single ``complete()`` method. Everything else — durability, retries, structured
output, guardrails, cost accounting — is the runner's job, so swapping vendors
is a one-line change at the ``Agent``.

Imports are lazy: the vendor SDKs are optional extras, and importing this
package must not require any of them.

    pip install loomsdk[anthropic]   # AnthropicProvider
    pip install loomsdk[openai]      # OpenAIProvider
    pip install loomsdk[gemini]      # GeminiProvider
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agents.providers.anthropic_provider import AnthropicProvider
    from loom.agents.providers.gemini_provider import GeminiProvider
    from loom.agents.providers.openai_provider import OpenAIProvider

__all__ = ["AnthropicProvider", "GeminiProvider", "OpenAIProvider", "from_env"]

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
        import_module(f"loom.agents.providers.{module}"), name
    )


#: Environment variable to provider, in the order they are tried. One list, so
#: "which vendor does this process talk to" has a single answer wherever it is
#: asked — the Runtime picking an agent backend and the coding agent picking a
#: model were reading the same three keys from two places.
_ENV_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_API_KEY", "AnthropicProvider"),
    ("OPENAI_API_KEY", "OpenAIProvider"),
    ("GEMINI_API_KEY", "GeminiProvider"),
)


def from_env() -> Any | None:
    """The first provider whose key is set and whose SDK imports, or ``None``.

    ``None`` is a normal answer, not an error: a process with no model key is a
    perfectly good Runtime, and most workflows never call a model. The caller
    that actually needs one says so — ``loom author`` reports which keys it
    looked for, which is more use than a stack trace from inside a vendor SDK.
    """
    import os

    for variable, provider_name in _ENV_PROVIDERS:
        if not os.environ.get(variable):
            continue
        try:
            return globals()["__getattr__"](provider_name)()
        except Exception:
            # A key set for an SDK that is not installed. Try the next one
            # rather than failing: the environment is telling us about a vendor
            # it uses elsewhere.
            continue
    return None


def env_keys() -> tuple[str, ...]:
    """The variables :func:`from_env` reads, for an error message to name."""
    return tuple(variable for variable, _ in _ENV_PROVIDERS)
