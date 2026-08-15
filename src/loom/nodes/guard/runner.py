"""Evaluating guards, and turning a verdict into engine behaviour.

The four verdicts already exist and mean the same thing they have always meant
— :class:`GuardrailAction` is imported, not redefined, because a second copy of
"what REJECT means" is a fork waiting to drift. What is new is *where* a guard
may attach: standalone via ``ctx.guard()``, around a node via
``NodeSpec.guards``, and where they already ran, around agent tool calls.

One semantic changes with the wider reach. In an agent loop REJECT hands the
model an explanation so it can adapt; in a workflow body there is nobody to
adapt, so a falsy return value would simply be ignored by the caller and the
guarded work would proceed anyway. Outside an agent loop, REJECT raises.

Two carriers, one meaning: agents pass the frozen dataclass
:class:`GuardrailResult`, nodes declare a Pydantic ``Output`` and so return
:class:`GuardVerdict`. Both carry the same :class:`GuardrailAction`, and
:func:`as_verdict` converts either into the other, so the runner below is the
only code that has to know both exist.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel, Field

from loom.agents.guardrails import (
    Guardrail,
    GuardrailAction,
    GuardrailResult,
)
from loom.core.exceptions import GuardrailTripwire
from loom.nodes.errors import GuardrailRejected

logger = logging.getLogger(__name__)

__all__ = ["GuardVerdict", "apply_guards", "as_verdict", "enforce"]


class GuardVerdict(BaseModel):
    """A guard node's answer. The Pydantic carrier of :class:`GuardrailResult`."""

    action: GuardrailAction = GuardrailAction.ALLOW
    message: str = ""
    replacement: Any = None
    info: Any = None

    @property
    def blocked(self) -> bool:
        return self.action in (GuardrailAction.REJECT, GuardrailAction.TRIPWIRE)

    @classmethod
    def allow(cls, info: Any = None) -> GuardVerdict:
        return cls(info=info)

    @classmethod
    def reject(cls, message: str, *, info: Any = None) -> GuardVerdict:
        return cls(action=GuardrailAction.REJECT, message=message, info=info)

    @classmethod
    def replace_with(cls, value: Any, *, message: str = "") -> GuardVerdict:
        return cls(action=GuardrailAction.REPLACE, replacement=value, message=message)

    @classmethod
    def tripwire(cls, message: str, *, info: Any = None) -> GuardVerdict:
        return cls(action=GuardrailAction.TRIPWIRE, message=message, info=info)


def as_verdict(value: Any) -> GuardVerdict:
    """Normalise whatever a guard returned into a :class:`GuardVerdict`.

    Accepts a ``GuardVerdict``, a ``GuardrailResult``, or a bare bool — the last
    because ``Guardrail.evaluate`` already treats ``True`` as "this is fine" and
    a guard node written by hand will do the same.
    """
    if isinstance(value, GuardVerdict):
        return value
    if isinstance(value, GuardrailResult):
        return GuardVerdict(
            action=value.action,
            message=value.message,
            replacement=value.replacement,
            info=value.info,
        )
    if isinstance(value, bool):
        return (
            GuardVerdict.allow()
            if value
            else GuardVerdict.reject("guard returned False")
        )
    raise TypeError(
        f"a guard must return GuardVerdict, GuardrailResult, or bool; got "
        f"{type(value).__name__}"
    )


def enforce(verdict: GuardVerdict, *, guard: str, value: Any) -> Any:
    """Apply *verdict* to *value*, or raise.

    Returns what the guarded work should see: the original value for ALLOW, the
    substitute for REPLACE. REJECT and TRIPWIRE do not return.
    """
    if verdict.action is GuardrailAction.ALLOW:
        return value
    if verdict.action is GuardrailAction.REPLACE:
        return verdict.replacement
    if verdict.action is GuardrailAction.REJECT:
        raise GuardrailRejected(guard, verdict.message or "no reason given", info=verdict.info)
    raise GuardrailTripwire(
        f"guardrail {guard!r} tripped: {verdict.message or 'no reason given'}",
        guardrail_name=guard,
        info=verdict.info,
    )


async def apply_guards(
    guard_ids: list[str],
    value: Any,
    *,
    ctx: Any,
    registry: Any,
    phase: str,
    subject: str,
    unwrap: bool = False,
) -> Any:
    """Run each guard in order and return the value the caller should use.

    Guards run **in declaration order** and a REPLACE feeds the next one, so a
    redaction guard followed by a policy guard sees redacted input — which is
    the only ordering that makes those two composable.

    A guard that raises is treated as a tripwire rather than an allow. A broken
    check has found nothing, and a check that cannot run must not open the gate;
    this is the same rule the verification pipeline applies when it reports a
    stage as *skipped* rather than passed.

    *unwrap* separates the two callers. ``ctx.guard("guard.pii", PiiIn(value=draft,
    redact=True))`` passes configuration and a subject in one object, and what
    the run should use afterwards is the **subject** — so ALLOW must return
    ``draft``, not the ``PiiIn``. Returning the wrapper made ALLOW and REPLACE
    hand back different types from the same call, which is worse than either.

    Node guards pass ``unwrap=False``: there the value *is* the node's payload,
    and a node whose Input happens to have a ``value`` field must still receive
    the whole thing.
    """
    current = _subject_of(value) if unwrap else value
    configured = value if unwrap else None
    for guard_id in guard_ids:
        try:
            # The first guard sees the configuration the caller supplied; a
            # later one sees whatever the previous guard left, so a redaction
            # followed by a policy check reads redacted input.
            checked = configured if configured is not None else current
            verdict = as_verdict(await _evaluate(guard_id, checked, ctx=ctx, registry=registry))
        except (GuardrailRejected, GuardrailTripwire):
            raise
        except Exception as exc:
            logger.warning("guard %s failed while guarding %s", guard_id, subject, exc_info=True)
            raise GuardrailTripwire(
                f"guardrail {guard_id!r} could not run while guarding {subject} "
                f"({phase}): {type(exc).__name__}: {exc}. A check that cannot run "
                "has found nothing, so it is not treated as a pass.",
                guardrail_name=str(guard_id),
            ) from exc
        current = enforce(verdict, guard=guard_id, value=current)
        configured = None
    return current


def _subject_of(value: Any) -> Any:
    """What is being checked, when the caller passed configuration around it.

    The guard-input convention is a ``value`` field; anything else is the
    subject itself.
    """
    if isinstance(value, BaseModel) and "value" in type(value).model_fields:
        # The stored field, not model_dump()["value"]: dumping would recursively
        # convert a nested model to a dict and hand the guarded work something
        # of a different type than the caller passed in.
        return value.__dict__.get("value")
    return value


async def _evaluate(guard: Any, value: Any, *, ctx: Any, registry: Any) -> Any:
    """Run one guard, whichever of the two forms it takes."""
    if isinstance(guard, Guardrail):
        return await guard.evaluate(value)
    if callable(guard) and not isinstance(guard, str):
        outcome: Any = guard(value)
        return await outcome if inspect.isawaitable(outcome) else outcome

    spec = registry.get(guard)
    if spec is None:
        from loom.nodes.base import near_matches
        from loom.nodes.errors import NodeNotFound

        raise NodeNotFound(str(guard), suggestions=near_matches(str(guard), registry.node_ids()))

    node = registry.resolve(str(guard))
    return await node.run(ctx, _payload_for(node.Input, value))


def _payload_for(model: type[BaseModel], value: Any) -> Any:
    """Fit *value* to the guard's declared ``Input``.

    Three cases, and the middle one is the whole reason this is a function.

    Already the right type — the caller configured the guard, as in
    ``ctx.guard("guard.pii", PiiIn(value=draft, redact=True))``. Pass it on.

    The guard declares the ``value`` convention — wrap. Validating the value
    *into* the model instead would silently succeed and leave ``value=None``:
    a ``BatchIn`` dumped flat has no ``value`` key, every other field is
    ignored as extra, and the guard is handed nothing while reporting a
    verdict. That is a guard that checks air.

    Neither — the guard declares its own shape, so validate into it and let a
    mismatch raise, which ``apply_guards`` turns into a tripwire.
    """
    if isinstance(value, model):
        return value
    if "value" in model.model_fields:
        return model.model_validate({"value": value})
    return model.model_validate(value)


class GuardInput(BaseModel):
    """The default shape a guard node receives when guarding an arbitrary value."""

    value: Any = Field(default=None, description="What is being checked.")
