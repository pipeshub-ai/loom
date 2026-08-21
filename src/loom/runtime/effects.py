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
    reversible: bool = False
    """Whether the effect can be undone by another operation.

    Defaults to ``False``: an operation nobody said was recoverable is not safe
    to assume is."""
    access_control: bool = False
    """Whether this changes who can reach data rather than the data itself."""
    asks_human: bool = False
    """Whether this call is the run *asking a person*, rather than acting.

    Set from a node that declares **both** ``requires=["human_channel"]`` and
    ``suspends`` — structural rather than a name match, so a project's own
    approval node is covered, and narrow because either claim alone is not the
    thing. A node that merely names a channel could be doing anything; one that
    also parks the run is waiting for a person, which is what this exempts.

    Read by ``TaintBroker``, which must never refuse one: the escape hatch that
    makes the taint rule usable is a human saying yes, and a rule that blocks
    the only way to ask is a deadlock rather than a policy.

    **What it does not do.** It exempts the *asking*, not the run. A write
    after the ask is weighed normally, and the recipient a workflow names in
    ``assignees`` is the channel's business — LOOM has always said it "claims
    nothing more about it" than what the channel reports. A host whose channel
    honours arbitrary recipients has a delivery path a tainted run can reach;
    that is a property of the channel, and it is asserted rather than implied
    in ``tests/test_taint.py``.
    """

    open_world: bool = True
    """Whether this reaches outside the deployment's trust boundary.

    Read by :class:`~loom.runtime.taint.TaintBroker`, which asks a different
    question of the same call than the grant does: not how much damage it can
    do, but whether what it returns came from somewhere nobody reviewed.
    Defaults to ``True`` for the same reason ``effect`` defaults to ``WRITE`` —
    an unclassified call is not safe to assume is self-contained."""
    run_id: str = ""
    path: str = ""
    """Journal path, so a denial can be traced to the exact call site."""
    name: str | None = None
    """The journal/grant name override, when the caller passed ``ctx.step(fn,
    name=...)`` — a bridged step whose semantic identity (``<toolset>.<op>``)
    differs from the Python function that implements it. Carried on the wire
    so a sandboxed body's proxied call journals and grant-checks under the
    same name an inline one would, rather than under the generic step's own
    name (e.g. ``pipeshub_tool`` for every bridged operation alike)."""
    local: bool = False
    """True when the child holds the implementation — a ``@step`` defined in
    the sandboxed source itself, which the parent must not ``exec()``. The
    parent still journals and grant-checks; the child runs the body. A name
    the parent does not recognise is still refused unless this is set, so a
    typo cannot become a silent delegation."""
    perform: Callable[[], Awaitable[Any]] | None = field(
        default=None, compare=False, repr=False
    )
    context: Any = field(default=None, compare=False, repr=False)
    """The workflow ``Context`` this call came from, when it came from one.

    Local-only, exactly like ``perform``, and excluded from equality and repr
    for the same reason: it is how the *local* side reaches back into the run,
    not part of what a call *is*. :meth:`describe` whitelists its fields, so the
    wire projection a sandboxed child sends is unaffected by this existing.

    A hook uses it to journal its own durable work beneath the call it is
    hooking — a supervisor or a critique that must be paid for once rather than
    on every retry."""

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
            "name": self.name,
            "local": self.local,
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



#: Node categories whose nodes reach nothing outside this process. Read from
#: the catalogue's own split between "a rule you can write today"
#: (control/transform) and "judgement or the outside world" (agent/io/human) —
#: the same distinction `NodeSpec.open_world` records per node.
_SELF_CONTAINED_NODE_CATEGORIES: frozenset[str] = frozenset({
    "control",
    "transform",
    "guard",
})

#: Effect kinds with no grant dimension of their own. Refused under `strict`,
#: unchecked otherwise — never silently permitted under a flag that promises
#: the opposite.
_UNDECLARED_KINDS: frozenset[str] = frozenset({"artifact", "event"})


#: The resource id workflow state is granted under. Reserved rather than derived
#: from the workflow name: a grant naming `state` means "this run may use its own
#: key-value space", which is one decision, not one per workflow.
STATE_RESOURCE = "state"


def _allows_resource(held: list[str], resource: str, effect: str) -> bool:
    """Whether *held* permits *effect* on *resource*.

    Entries are ``name`` or ``name:effect``. A bare name permits every effect on
    it; a qualified one permits that effect and, for ``write``, the ``read`` it
    implies — a caller allowed to write state that could not read it back would
    be a grant nobody would write on purpose.
    """
    # What each *held* qualifier permits, not what each effect requires. Written
    # the other way round first, which read plausibly and inverted the whole
    # check: `state:read` permitted writes and `state:write` refused reads.
    permits = {
        "read": {"read"},
        "write": {"read", "write"},
        "destructive": {"read", "write", "destructive"},
    }
    for entry in held:
        name, _, qualifier = entry.partition(":")
        if name != resource:
            continue
        if not qualifier:
            return True
        if effect in permits.get(qualifier, {qualifier}):
            return True
    return False

class _CallCeiling:
    """A bounded counter reserved before the work, not credited after it.

    Its own class rather than two attributes on the broker, because the
    ordering is the whole correctness argument and it deserves somewhere to be
    stated and tested on its own: a ceiling that counts *completions* bounds
    nothing under concurrency, since every in-flight call reads the count
    before any of them writes it.

    Not thread-safe, and does not need to be: a broker is dispatched from one
    event loop, and within one loop `reserve()` is atomic because it contains
    no await.
    """

    __slots__ = ("_used", "limit")

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self._used >= self.limit

    def reserve(self) -> bool:
        """Claim a slot. ``False`` when there is none left, having claimed nothing."""
        if self.exhausted:
            return False
        self._used += 1
        return True

    def release(self) -> None:
        """Hand a slot back — for a reservation whose work never started."""
        self._used = max(0, self._used - 1)

    def refusal(self, target: str) -> EffectResult:
        return EffectResult(
            ok=False,
            error=(
                f"call ceiling reached: {self.limit} effects already "
                f"performed, refusing '{target}'"
            ),
            needs=f"max_calls > {self.limit}",
        )

    def __repr__(self) -> str:
        ceiling = "unbounded" if self.limit is None else str(self.limit)
        return f"<_CallCeiling {self._used}/{ceiling}>"


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
        self._ceiling = _CallCeiling(max_calls)

    @property
    def max_calls(self) -> int | None:
        """Effects this broker will dispatch before refusing. ``None`` is
        unbounded."""
        return self._ceiling.limit

    @property
    def dispatched(self) -> int:
        """How many have been reserved. Refusals do not count — a run should
        not be able to exhaust its own budget by being denied."""
        return self._ceiling.used

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        refusal = self._refuse(call, authority)
        if refusal is not None:
            return refusal
        if call.perform is None:
            return EffectResult(ok=False, error=f"nothing to perform for {call.target}")
        # Reserved *before* the await, never after it. Everything above this
        # line is synchronous, so the check in `_refuse` and this reservation
        # happen in one uninterrupted event-loop step and no second coroutine
        # can observe the pre-reservation count. Counting after `perform()`
        # returned made the ceiling a no-op under `ctx.gather`: every branch
        # read `dispatched` before any branch wrote it, so ten concurrent
        # calls all passed a ceiling of three.
        if not self._ceiling.reserve():  # pragma: no cover - _refuse covers it
            return self._ceiling.refusal(call.target)
        value = await call.perform()
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

        if self._ceiling.exhausted:
            return self._ceiling.refusal(call.target)

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

        if call.kind == "state":
            # `ctx.state` is a durable, cross-run key-value space, so it is a
            # *resource* in the sense `resources` already names — and until it
            # dispatched here, `resources` was a dimension nothing ever read: a
            # declaration that looked enforced and was not.
            #
            # `strict` alone closes it, as for every other dimension. A grant
            # that lists resources without naming `state` refuses state, which
            # is the "within a dimension it spoke to" rule applied consistently.
            if not grant.resources:
                if grant.strict:
                    return self._denied(
                        call, f"'{call.target}' is not granted",
                        needs=f"{STATE_RESOURCE}:{call.effect.value}", held=[],
                    )
                return None
            if _allows_resource(grant.resources, STATE_RESOURCE, call.effect.value):
                return None
            return self._denied(
                call, f"'{call.target}' is not granted",
                needs=f"{STATE_RESOURCE}:{call.effect.value}",
                held=grant.resources,
            )

        if call.kind == "node":
            # Categories that reach nothing outside the process are permitted
            # without an entry, even under `strict`. `control.switch` is a
            # comparison and `transform.template` is string formatting; making
            # a workflow enumerate them to be allowed to compute would turn
            # `strict` into something nobody switches on. The categories that
            # *do* reach out — io, agent, human — need saying.
            if call.target.partition(".")[0] in _SELF_CONTAINED_NODE_CATEGORIES:
                return None
            if not grant.nodes:
                if grant.strict:
                    return self._denied(
                        call, f"node '{call.target}' is not granted",
                        needs=call.target, held=[],
                    )
                return None
            if grant.allows_node(call.target):
                return None
            return self._denied(
                call, f"node '{call.target}' is not granted",
                needs=call.target, held=grant.nodes,
            )

        if call.kind in _UNDECLARED_KINDS:
            # `artifact` and `event` had no branch at all, so `strict` — whose
            # whole promise is "every dimension is deny-by-default, declared or
            # not" — left them open. There is no grant dimension for either
            # yet; until there is, strict refuses rather than silently
            # permitting, which is the direction the flag exists to fail in.
            if grant.strict:
                return self._denied(
                    call, f"{call.kind} '{call.target}' is not granted",
                    needs=f"{call.kind}:{call.target}", held=[],
                )
            return None

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
