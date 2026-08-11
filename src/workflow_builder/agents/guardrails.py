"""Guardrails at three scopes: agent input, agent output, and individual tool calls.

Tool-level guardrails matter most. Screening the final answer is too late if the model has
already issued a destructive call, so the interesting checks run between "the model asked
for this" and "we did it".
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GuardrailAction(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    """Block the call and hand the model an explanation so it can adapt."""
    REPLACE = "replace"
    """Substitute sanitised content and continue."""
    TRIPWIRE = "tripwire"
    """Abort the run entirely."""


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """The verdict from one guardrail."""

    action: GuardrailAction = GuardrailAction.ALLOW
    message: str = ""
    replacement: Any = None
    info: Any = None

    @property
    def blocked(self) -> bool:
        return self.action in (GuardrailAction.REJECT, GuardrailAction.TRIPWIRE)


def allow(info: Any = None) -> GuardrailResult:
    return GuardrailResult(info=info)


def reject(message: str, *, info: Any = None) -> GuardrailResult:
    """Refuse this call but let the agent continue and try something else."""
    return GuardrailResult(action=GuardrailAction.REJECT, message=message, info=info)


def replace_with(value: Any, *, message: str = "") -> GuardrailResult:
    return GuardrailResult(action=GuardrailAction.REPLACE, replacement=value, message=message)


def tripwire(message: str, *, info: Any = None) -> GuardrailResult:
    """Stop the whole run. Reserve for policy violations, not recoverable mistakes."""
    return GuardrailResult(action=GuardrailAction.TRIPWIRE, message=message, info=info)


GuardrailFn = Callable[..., "GuardrailResult | Awaitable[GuardrailResult]"]


@dataclass
class Guardrail:
    """A named check bound to a scope."""

    fn: GuardrailFn
    name: str
    blocking: bool = True
    """When False the check runs alongside the model call instead of gating it.

    Non-blocking is faster but the expensive model may have already produced tokens by the
    time the verdict lands.
    """
    metadata: dict[str, Any] = field(default_factory=dict)

    async def evaluate(self, *args: Any, **kwargs: Any) -> GuardrailResult:
        outcome = self.fn(*args, **kwargs)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if not isinstance(outcome, GuardrailResult):
            # A bare bool is a common shorthand: True means "this is fine".
            return allow() if outcome else reject(f"guardrail '{self.name}' rejected the value")
        return outcome

    def __repr__(self) -> str:
        return f"<guardrail {self.name}>"


def guardrail(
    fn: GuardrailFn | None = None,
    /,
    *,
    name: str | None = None,
    blocking: bool = True,
) -> Any:
    """Declare a guardrail.

    ```python
    @guardrail
    def no_secrets(arguments: dict) -> GuardrailResult:
        if "sk-" in str(arguments):
            return reject("Remove API keys before calling this tool.")
        return allow()
    ```
    """

    def decorate(target: GuardrailFn) -> Guardrail:
        return Guardrail(
            fn=target, name=name or getattr(target, "__name__", "guardrail"), blocking=blocking
        )

    if fn is not None:
        return decorate(fn)
    return decorate
