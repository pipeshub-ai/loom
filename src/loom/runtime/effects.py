"""The seam every durable operation passes through.

A workflow body reaches the outside world only through `Context`, and `Context`
performs its work inline. That is fine while the code is trusted and the
deployment is one tenant, and it is the wrong shape the moment either stops
being true — there is nowhere to ask "is this call allowed?", and nowhere for a
sandboxed child to send a call it is not permitted to make itself.

So the call gets a name and a mediator:

`EffectCall`
    A description of one operation: what kind, on what, with what arguments,
    and whether it reads or writes. Serialisable, deliberately — the same
    object that a local broker inspects is the one a remote broker receives.
`EffectBroker`
    Decides what happens to it. `DirectBroker` performs it; `GuardedBroker`
    checks a `Grant` first, counts it against a ceiling, and refuses writes in
    a dry run.

Two payoffs from one mechanism. Enforcement is the visible one. The other is
that a subprocess backend has a channel to speak across without inventing a
second vocabulary for it — the child describes an effect, the parent dispatches
it, and the parent is where authority lives.

The default costs nothing:

>>> import asyncio
>>> from loom.runtime.effects import DirectBroker, EffectCall
>>> from loom.security.authority import Authority
>>> async def work() -> str:
...     return "done"
>>> call = EffectCall(kind="step", target="greet", perform=work)
>>> asyncio.run(DirectBroker().dispatch(call, Authority())).value
'done'
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loom.core.exceptions import WorkflowError
from loom.security.authority import Authority
from loom.toolsets.manifest import EffectClass

__all__ = [
    "DirectBroker",
    "EffectBroker",
    "EffectCall",
    "EffectDenied",
    "EffectResult",
    "GuardedBroker",
    "RunObserver",
]


class EffectDenied(WorkflowError):  # noqa: N818
    """A broker refused an effect.

    Carries what was refused and what would have allowed it. A denial that only
    says "denied" turns into a support ticket; one that names the missing grant
    turns into a one-line fix.
    """

    def __init__(self, message: str, *, call: EffectCall, needs: str = "") -> None:
        super().__init__(message)
        self.call = call
        self.needs = needs
        """The grant that would have permitted this call, when one exists."""


@dataclass(frozen=True)
class EffectCall:
    """One durable operation a workflow body asked for.

    Everything except `perform` is plain data, so a broker can log it, decide on
    it, or send it somewhere else. `perform` is how the *local* side would carry
    it out; a broker that dispatches elsewhere ignores it, which is why it is
    excluded from equality and repr.
    """

    kind: str
    """``step``, ``agent``, ``child``, ``tool``, ``artifact``, ``event`` — what
    sort of thing this is, which decides which part of a grant applies."""
    target: str
    """What it acts on. A step name, an agent name, a workflow name, or
    ``<toolset>.<operation>`` for a tool call."""
    arguments: dict[str, Any] = field(default_factory=dict)
    effect: EffectClass = EffectClass.WRITE
    """Read, write, or destructive. Defaults to write: an operation whose class
    nobody declared is not safe to assume is harmless."""
    run_id: str = ""
    path: str = ""
    """Journal path, so a denial can be traced to the exact call site."""
    perform: Callable[[], Awaitable[Any]] | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def toolset(self) -> str:
        """The toolset half of a ``<toolset>.<operation>`` target, else ``""``."""
        return self.target.partition(".")[0] if self.kind == "tool" else ""

    @property
    def operation(self) -> str:
        """The operation half of a ``<toolset>.<operation>`` target."""
        return self.target.partition(".")[2] if self.kind == "tool" else ""

    @property
    def writes(self) -> bool:
        return self.effect is not EffectClass.READ

    def describe(self) -> dict[str, Any]:
        """The wire projection: everything a remote broker needs, nothing more."""
        return {
            "kind": self.kind,
            "target": self.target,
            "arguments": self.arguments,
            "effect": self.effect.value,
            "run_id": self.run_id,
            "path": self.path,
        }


@dataclass(frozen=True)
class EffectResult:
    """What came back. ``ok=False`` carries the reason instead of raising.

    A broker returns rather than raises so a caller can decide how a refusal
    surfaces — as a step failure, as a message back to an agent, or as a denial
    the workflow handles itself.
    """

    ok: bool = True
    value: Any = None
    error: str | None = None
    needs: str = ""
    """The grant that would have allowed a refused call."""

    def unwrap(self) -> Any:
        """The value, or the refusal as an exception."""
        if self.ok:
            return self.value
        raise EffectDenied(
            self.error or "effect denied",
            call=EffectCall(kind="", target=""),
            needs=self.needs,
        )


@runtime_checkable
class EffectBroker(Protocol):
    """Mediates every durable operation a workflow performs."""

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        """Carry out *call* under *authority*, or refuse it."""
        ...


@runtime_checkable
class RunObserver(Protocol):
    """A broker that keeps per-run state, and needs the run's history to do it.

    Optional, and checked structurally: most brokers decide on one call at a
    time and implement nothing here. It exists for the ones whose decision
    depends on what the run has *already* done — taint being the example — and
    those cannot be correct without it. The engine serves a journaled call from
    the journal without dispatching it, so a broker that accumulated its state
    from dispatches alone would see an empty history after any re-entry and,
    having seen nothing, permit everything.

    A broker that wraps another must forward both calls, or the wrapped one goes
    back to being re-entry-blind.
    """

    def observe_run(self, run_id: str, journal: Any) -> None:
        """Rebuild per-run state from the journal, before the body re-enters."""
        ...

    def forget_run(self, run_id: str) -> None:
        """Drop a terminal run's state."""
        ...


class DirectBroker:
    """Performs every effect, checks nothing. The default.

    Exists so that routing through a broker is not a feature you opt into: the
    seam is always there, and a deployment that needs no policy pays only an
    attribute lookup and a call for it. Anything more expensive here would push
    people to bypass the seam, which defeats its purpose.
    """

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        if call.perform is None:
            return EffectResult(ok=False, error=f"nothing to perform for {call.target}")
        return EffectResult(value=await call.perform())

    def __repr__(self) -> str:
        return "<DirectBroker>"


class GuardedBroker:
    """Enforces an authority on every dispatch.

    Three checks, in increasing cost:

    1. **Dry run** — a write is refused outright. Reads proceed, so a dry run
       is a real rehearsal against real data rather than a mock.
    2. **Call ceiling** — ``max_calls`` bounds how many effects one run may
       perform. A workflow that loops on a tool call otherwise burns budget
       until something else notices.
    3. **Grant** — the authority's :class:`GrantSet` must permit this specific
       operation, checked *here* rather than when tools were handed out. That
       distinction is the point: a grant checked once at resolution can be
       outlived by the reference it produced, and an agent that kept a tool
       object keeps the authority with it.

    Counting is per broker instance, so one instance is one budget. Construct
    one per run to bound a run; share one to bound a worker.
    """

    def __init__(self, *, max_calls: int | None = None) -> None:
        self.max_calls = max_calls
        """Effects this broker will dispatch before refusing. ``None`` is
        unbounded."""
        self.dispatched = 0
        """How many have been performed. Refusals do not count — a run should
        not be able to exhaust its own budget by being denied."""

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        refusal = self._refuse(call, authority)
        if refusal is not None:
            return refusal
        if call.perform is None:
            return EffectResult(ok=False, error=f"nothing to perform for {call.target}")
        value = await call.perform()
        self.dispatched += 1
        return EffectResult(value=value)

    def _refuse(self, call: EffectCall, authority: Authority) -> EffectResult | None:
        """The reason this call cannot proceed, or ``None``."""
        if authority.dry_run and call.writes:
            return EffectResult(
                ok=False,
                error=(
                    f"dry run: refused {call.effect.value} operation "
                    f"'{call.target}'. Reads proceed; writes do not."
                ),
            )

        if self.max_calls is not None and self.dispatched >= self.max_calls:
            return EffectResult(
                ok=False,
                error=(
                    f"call ceiling reached: {self.max_calls} effects already "
                    f"performed, refusing '{call.target}'"
                ),
                needs=f"max_calls > {self.max_calls}",
            )

        return self._check_grant(call, authority)

    def _check_grant(
        self, call: EffectCall, authority: Authority
    ) -> EffectResult | None:
        """Apply the part of the grant that governs this kind of call.

        An empty grant set permits everything: declaring no permissions is not
        the same as declaring that none are permitted, and treating it as the
        latter would break every existing Runtime the moment a broker was
        configured. Deny-by-default applies within a dimension the caller has
        spoken to — declare ``toolsets`` and every unlisted toolset is refused.

        That "within a dimension it spoke to" rule has a hole: a grant that
        declares only ``toolsets`` leaves ``agent`` and ``child`` calls
        completely unchecked, because ``grant.agents`` being empty reads as
        "nothing to check" rather than "nothing permitted". ``grant.strict``
        closes it — set on an identity-derived grant, it means every
        dimension is deny-by-default, declared or not.
        """
        grant = authority.grant
        if grant.is_empty:
            return None

        if call.kind == "tool":
            if not grant.toolsets:
                if grant.strict:
                    return self._denied(
                        call, f"'{call.target}' is not granted",
                        needs=f"{call.toolset}.{call.operation}:{call.effect.value}",
                        held=[],
                    )
                return None
            if grant.allows_operation(call.toolset, call.operation, call.effect.value):
                return None
            return self._denied(
                call,
                f"'{call.target}' is not granted",
                needs=f"{call.toolset}.{call.operation}:{call.effect.value}",
                held=grant.toolsets,
            )

        if call.kind == "step" and "." in call.target:
            # A bridged step whose semantic name is `<toolset>.<operation>`
            # (see `Context.step(fn, name="jira.create_issue", ...)`) is a
            # tool call in every way that matters to a grant -- only the
            # journal calls it "step" because it was dispatched through
            # `ctx.step()` rather than a native `Toolset`. Without this, a
            # host bridging a dynamic tool registry (one `@step` wrapping
            # many operations, since the operations are not known until a
            # task resolves its tools) could never be grant-checked: the
            # existing "tool" branch only ever sees `kind == "tool"`, which
            # native Loom toolsets produce and bridged ones do not.
            # Undotted step names (an ordinary local `@step`) fall through
            # unchecked, exactly as before this branch existed.
            toolset_id, _, op_id = call.target.partition(".")
            if not grant.toolsets:
                if grant.strict:
                    return self._denied(
                        call, f"'{call.target}' is not granted",
                        needs=f"{call.target}:{call.effect.value}", held=[],
                    )
                return None
            if grant.allows_operation(toolset_id, op_id, call.effect.value):
                return None
            return self._denied(
                call,
                f"'{call.target}' is not granted",
                needs=f"{call.target}:{call.effect.value}",
                held=grant.toolsets,
            )

        if call.kind == "agent":
            if not grant.agents:
                if grant.strict:
                    return self._denied(
                        call, f"agent '{call.target}' is not granted",
                        needs=call.target, held=[],
                    )
                return None
            if call.target in grant.agents:
                return None
            return self._denied(
                call, f"agent '{call.target}' is not granted",
                needs=call.target, held=grant.agents,
            )

        if call.kind == "child":
            if not grant.subflows:
                if grant.strict:
                    return self._denied(
                        call, f"sub-workflow '{call.target}' is not granted",
                        needs=call.target, held=[],
                    )
                return None
            if call.target in grant.subflows:
                return None
            return self._denied(
                call, f"sub-workflow '{call.target}' is not granted",
                needs=call.target, held=grant.subflows,
            )

        return None

    @staticmethod
    def _denied(
        call: EffectCall, what: str, *, needs: str, held: list[str]
    ) -> EffectResult:
        holding = ", ".join(held) or "nothing"
        return EffectResult(
            ok=False,
            error=f"{what}. Held: {holding}. Add '{needs}' to the grant to allow it.",
            needs=needs,
        )

    def __repr__(self) -> str:
        ceiling = "unbounded" if self.max_calls is None else str(self.max_calls)
        return f"<GuardedBroker {self.dispatched}/{ceiling}>"
