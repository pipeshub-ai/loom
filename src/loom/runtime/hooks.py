"""Middleware around every durable operation.

A hook is a named point where the engine hands control to code the host
registered, and takes it back. This module is the kernel: registration,
routing, composition, and the decision model. Where it attaches is
:class:`HookBroker`, which composes into the existing broker chain.

**One primitive, three shapes.** Everything here compiles to a single form —
``async def (ctx, next) -> Any``, the onion — because the other two shapes are
that one with a fixed structure:

    before f  ->  async def w(ctx, next): await f(ctx); return await next()
    after  f  ->  async def w(ctx, next): r = await next(); await f(ctx); return r

Composing onions gives the required ordering for free rather than by
enforcement: with ``w1(w2(w3(inner)))``, the *before* halves run 1→2→3 and the
*after* halves run 3→2→1. Nobody has to remember that ``after`` is reversed; it
is what nesting means.

**The safety properties are compiled in, not asked for.** Two bugs that this
shape makes unavailable:

*A forgotten* ``next()``. A sequential middleware that fails to continue the
chain silently drops every middleware behind it, and the failure is invisible
until the one nobody tested does not run. ``before`` and ``after`` therefore do
not receive ``next`` at all — the wrapper calls it. Only ``around`` does, because
re-invoking it is the entire reason that shape exists.

*A weakened decision.* A middleware cannot assign a decision; it calls
:meth:`HookContext.deny` or :meth:`HookContext.ask`, which escalate along
``allow < ask < deny`` and never descend. So a permissive middleware registered
after a strict one cannot lift its refusal, whatever order they were added in.

**Replay is not a concern here, by construction.** ``DurableCall._resolve``
serves a completed entry from the journal *before* reaching the broker, so a
hook in this family never sees a replayed call. That is why these hooks may
perform I/O and may refuse. Hooks that run on every body re-entry are a
different family with different rules, and are deliberately not in this module.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import Any, TypeVar

from loom.runtime.effects import EffectBroker, EffectCall, EffectResult, RunObserver
from loom.security.authority import Authority
from loom.toolsets.manifest import EffectClass

logger = logging.getLogger("workflow.hooks")

__all__ = [
    "AgentHookContext",
    "BodyContext",
    "Decision",
    "HookBroker",
    "HookContext",
    "HookRegistry",
    "Next",
]

Next = Callable[[], Awaitable[Any]]
"""The rest of the chain. Call it once, many times, or not at all."""

Wrap = Callable[["HookContext", Next], Awaitable[Any]]

F = TypeVar("F", bound=Callable[..., Any])

#: Events, keyed by the ``EffectCall.kind`` they select. ``node`` is absent
#: deliberately — see :func:`_is_node`.
_KINDS = ("step", "tool", "agent", "child", "artifact", "event")

#: How ``ctx.node`` names itself in the journal. Nodes are journaled as steps
#: (``DurableCall(kind=EntryKind.STEP, name=f"node:{node_id}")``), so routing
#: derives the event from the name rather than the kind. The alternative was a
#: new ``EntryKind``, which would change the journal format for runs already on
#: disk to buy nothing a prefix check does not.
_NODE_PREFIX = "node:"

#: The agent family, in the order they fire within one run.
_AGENT_EVENTS = (
    "agent_start",
    "turn_start",
    "model_start",
    "model_end",
    "turn_end",
    "agent_end",
)


class Decision(IntEnum):
    """What a hook concluded about a call.

    An ``IntEnum`` so escalation is ``max()`` and the ordering is the type's
    own, rather than a severity table kept beside it that can disagree.
    """

    ALLOW = 0
    ASK = 1
    """Park the run on a human before proceeding."""
    DENY = 2
    """Refuse. Journaled as a failure, so a replay sees what the run saw."""


class HookContext:
    """One durable operation, as a hook sees it.

    Carries the call, the decision so far, and — for ``after`` — the result.
    Mutating :attr:`arguments` changes what the operation receives; assigning
    :attr:`result` changes what the caller gets back.
    """

    __slots__ = (
        "_decision",
        "_error",
        "_reason",
        "_refused_by",
        "_result",
        "call",
        "metadata",
    )

    def __init__(self, call: EffectCall) -> None:
        self.call = call
        self.metadata: dict[str, Any] = {}
        """Free-form, and carried from ``before`` to ``after`` on the same
        context object — a risk score computed once and read later."""
        self._decision = Decision.ALLOW
        self._reason = ""
        self._refused_by = ""
        self._result: Any = None
        self._error: BaseException | None = None

    # -- what is being called -------------------------------------------------

    @property
    def kind(self) -> str:
        return self.call.kind

    @property
    def target(self) -> str:
        """Step name, ``<toolset>.<operation>``, agent name, or workflow name."""
        return self.call.target

    @property
    def node_id(self) -> str:
        """The node id when this is a node call, else ``""``."""
        return (
            self.call.target[len(_NODE_PREFIX) :]
            if _is_node(self.call)
            else ""
        )

    @property
    def arguments(self) -> dict[str, Any]:
        """Mutable. What the operation will receive."""
        return self.call.arguments

    @property
    def effect(self) -> EffectClass:
        return self.call.effect

    @property
    def run_id(self) -> str:
        return self.call.run_id

    @property
    def path(self) -> str:
        """Journal path of the call being hooked."""
        return self.call.path

    @property
    def ctx(self) -> Any:
        """A context whose durable calls journal beneath this one.

        Scoped to ``<path>#hook``, which is derived from the hooked call rather
        than from a sequence counter — so a hook that runs
        ``await ctx.step(...)`` gets a *stable* path and its work is journaled
        once, not repeated on every retry and resume. That is what makes a
        supervisor or a critique affordable: the model is paid for once.

        ``None`` when the call did not come from a workflow body.
        """
        parent = getattr(self.call, "context", None)
        return parent.nested(f"{self.path}#hook") if parent is not None else None

    # -- the outcome ----------------------------------------------------------

    @property
    def result(self) -> Any:
        """What the operation returned. Only meaningful in ``after``."""
        return self._result

    @result.setter
    def result(self, value: Any) -> None:
        self._result = value

    @property
    def error(self) -> BaseException | None:
        """What the operation raised, when it did. ``after`` runs either way."""
        return self._error

    # -- deciding -------------------------------------------------------------

    @property
    def decision(self) -> Decision:
        return self._decision

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def refused_by(self) -> str:
        """Which middleware escalated. Recorded so a denial names its author."""
        return self._refused_by

    @property
    def settled(self) -> bool:
        """True once a decision has been reached that stops the chain."""
        return self._decision is not Decision.ALLOW

    def allow(self) -> None:
        """Explicitly permit. A no-op, and the default — present so a hook can
        say nothing-to-see rather than trailing off."""

    def ask(self, reason: str = "") -> None:
        """Require a human before this call proceeds."""
        self._escalate(Decision.ASK, reason)

    def deny(self, reason: str = "") -> None:
        """Refuse this call."""
        self._escalate(Decision.DENY, reason)

    def _escalate(self, decision: Decision, reason: str, by: str = "") -> None:
        """Raise the decision, never lower it.

        ``max`` rather than assignment is the whole guarantee: registration
        order cannot weaken policy, so a permissive middleware added later
        beside a strict one is a no-op rather than a silent hole.
        """
        if decision <= self._decision:
            return
        self._decision = decision
        self._reason = reason
        self._refused_by = by or self._refused_by

    def __repr__(self) -> str:
        return (
            f"<HookContext {self.kind}:{self.target} "
            f"{self._decision.name.lower()}>"
        )


class BodyContext:
    """One entry into a workflow body, as a hook sees it.

    **This type has no ``deny`` and no ``ask``, and that absence is the design
    rather than an omission.** Unlike an effect hook, a body hook *does* run on
    replay — the body is re-entered on every resume, retry, retry-after-park and
    orphan reclaim, while the calls inside it are served from the journal. So
    the middleware installed *now* is not necessarily the middleware that
    produced the run, and a body hook that could refuse would let a replay
    re-derive an outcome from configuration that has since changed.

    Being unable to decide is what makes that harmless: different middleware on
    a replay can change what is *observed*, never what happened. It is also what
    lets middleware stay out of the workflow version — see
    ``docs/design/hooks-middleware.md``, Q10.

    So: log here, count here, trace here. To gate something, hook the effect.
    """

    __slots__ = (
        "attempt",
        "error",
        "input",
        "metadata",
        "output",
        "re_entry",
        "run_id",
        "status",
        "workflow",
    )

    def __init__(
        self,
        *,
        run_id: str,
        workflow: str,
        attempt: int,
        re_entry: bool,
        input: Any = None,
        status: str = "",
        output: Any = None,
        error: BaseException | None = None,
    ) -> None:
        self.run_id = run_id
        self.workflow = workflow
        self.attempt = attempt
        self.re_entry = re_entry
        """True when the body has been entered before for this run.

        Derived from the journal having entries, which is the honest signal:
        it is true for a resume, a retry and a replay alike, and those are the
        cases where "the workflow started" would be a lie. Temporal exposes the
        same idea as ``workflow.unsafe.is_read_only()``.
        """
        self.input = input
        self.status = status
        """``completed``, ``suspended``, ``cancelled``, ``failed``, or
        ``abandoned`` — only on end."""
        self.output = output
        self.error = error
        self.metadata: dict[str, Any] = {}

    def __repr__(self) -> str:
        where = "re-entry" if self.re_entry else "start"
        return f"<BodyContext {self.workflow}/{self.run_id} {where}>"


class AgentHookContext:
    """A point inside one agent run, as a hook sees it.

    **Observational and mutational, never decisional** — and unlike
    :class:`BodyContext`, that is not because of replay. It is because the
    decisions are already taken elsewhere: "may this agent run?" is an effect
    hook on ``kind="agent"``, and "may this tool call run?" is an effect hook on
    ``kind="tool"``, because a tool call inside the loop goes through the broker
    like everything else. Adding a second way to refuse the same two things is
    exactly the duplication this whole design exists to avoid.

    What is left is real and is most of what middleware is actually used for:
    shaping the messages before a model call, observing responses, counting
    turns, and deciding the loop has gone far enough.

    Replay-free by containment: an agent run is a single journal entry, so a
    completed one is served from the journal and the loop never re-enters.
    """

    __slots__ = (
        "agent_name",
        "error",
        "input",
        "messages",
        "metadata",
        "response",
        "result",
        "run_id",
        "stop_reason",
        "stopped",
        "tools",
        "turn",
    )

    def __init__(
        self,
        *,
        agent_name: str = "",
        run_id: str = "",
        turn: int = 0,
        input: Any = None,
        messages: list[Any] | None = None,
        response: Any = None,
        result: Any = None,
        tools: list[str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.run_id = run_id
        self.turn = turn
        self.input = input
        self.messages = messages if messages is not None else []
        """**Mutable, in place.** The hook point for compaction, sliding
        windows, redaction and trimming — the single largest category of
        middleware in every system surveyed. Rebinding this does nothing;
        mutate it."""
        self.response = response
        self.result = result
        self.tools = tools or []
        self.error = error
        self.metadata: dict[str, Any] = {}
        self.stopped = False
        self.stop_reason = ""

    def stop(self, reason: str = "") -> None:
        """End the turn loop after this turn.

        Not a refusal — nothing is denied and nothing is undone. It says the
        loop has gone far enough, which is what a stall detector or a spend
        ceiling wants and what ``deny`` would describe badly. The loop raises
        :class:`~loom.core.exceptions.AgentStopped` at the top of the next turn,
        because its only other early exit — the turn budget — raises too, and a
        partial result handed back quietly would make "something gave up on
        this" indistinguishable from "the agent finished".

        **The first stop wins**, reason included, matching the escalate-only
        rule the decision model uses: a later middleware cannot restate why the
        loop is ending, so what an operator reads is the check that actually
        fired rather than whichever ran last.
        """
        if self.stopped:
            return
        self.stopped = True
        self.stop_reason = reason

    def __repr__(self) -> str:
        return f"<AgentHookContext {self.agent_name} turn={self.turn}>"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


Matcher = Callable[[EffectCall], bool]


def _is_node(call: EffectCall) -> bool:
    return call.kind == "step" and call.target.startswith(_NODE_PREFIX)


def _build_matcher(
    event: str,
    *,
    target: str = "",
    node: str = "",
    effect: EffectClass | set[EffectClass] | None = None,
    where: Callable[[HookContext], bool] | None = None,
) -> Matcher:
    """Compile the routing arguments into one predicate, once, at registration.

    Cheapest test first: the event selects on an interned string, so a hook
    registered for tools costs one comparison on every step.
    """
    effects = (
        {effect}
        if isinstance(effect, EffectClass)
        else set(effect)
        if effect
        else None
    )

    def matches(call: EffectCall) -> bool:
        if event == "node":
            if not _is_node(call):
                return False
        elif event != "*":
            if call.kind != event:
                return False
            # A node is journaled as a step; a hook asking for steps should not
            # silently also get every node.
            if event == "step" and _is_node(call):
                return False
        if target and not fnmatch.fnmatchcase(call.target, target):
            return False
        if node and not fnmatch.fnmatchcase(
            call.target[len(_NODE_PREFIX) :] if _is_node(call) else "", node
        ):
            return False
        if effects is not None and call.effect not in effects:
            return False
        return where is None or where(HookContext(call))

    return matches


# ---------------------------------------------------------------------------
# Compiling the three shapes onto one
# ---------------------------------------------------------------------------


def _from_before(fn: Callable[[HookContext], Awaitable[None]], label: str) -> Wrap:
    """``before``: run, then continue *unless* a decision stopped the chain.

    The decision check lives here rather than in the middleware, so refusing is
    one call and cannot be followed by an accidental continuation.

    Fails **closed**: a middleware that raises has not passed, and a gate that
    could not run has found nothing. Same rule `CheckPipeline` applies to a
    missing linter and `Guardrail` applies to a check that raises.
    """

    async def wrapped(ctx: HookContext, next: Next) -> Any:
        try:
            await fn(ctx)
        except Exception as exc:
            logger.exception("hook %s failed before %s", label, ctx.target)
            ctx._escalate(Decision.DENY, f"hook {label!r} failed: {exc}", by=label)
        if ctx.settled:
            ctx._refused_by = ctx._refused_by or label
            return None
        return await next()

    return wrapped


def _from_after(fn: Callable[[HookContext], Awaitable[None]], label: str) -> Wrap:
    """``after``: continue, then run — on success *and* on failure.

    Fails **open**. The work already happened; a broken formatter or a logger
    with a bad format string must not destroy a valid result. The error is
    recorded rather than raised.
    """

    async def wrapped(ctx: HookContext, next: Next) -> Any:
        try:
            ctx._result = await next()
        except Exception as exc:
            ctx._error = exc
            await _observe(fn, ctx, label)
            raise
        await _observe(fn, ctx, label)
        return ctx._result

    return wrapped


async def _observe(
    fn: Callable[[HookContext], Awaitable[None]], ctx: HookContext, label: str
) -> None:
    try:
        await fn(ctx)
    except Exception as exc:
        logger.exception("hook %s failed after %s", label, ctx.target)
        ctx.metadata.setdefault("hook_errors", []).append(f"{label}: {exc}")


def _compose(wraps: list[Wrap], ctx: HookContext, inner: Next) -> Next:
    """Nest *wraps* around *inner*, first registered outermost."""
    handler = inner
    for wrap in reversed(wraps):
        handler = _bind(wrap, ctx, handler)
    return handler


def _bind(wrap: Wrap, ctx: HookContext, inner: Next) -> Next:
    async def bound() -> Any:
        return await wrap(ctx, inner)

    return bound


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _Registration:
    __slots__ = ("label", "matches", "wrap")

    def __init__(self, matches: Matcher, wrap: Wrap, label: str) -> None:
        self.matches = matches
        self.wrap = wrap
        self.label = label


class HookRegistry:
    """Middleware registered on one Runtime.

    **Deliberately not chained to a process-global registry**, unlike
    ``toolsets`` and ``nodes``. Those answer "what integrations exist?", which is
    a property of the installation. Middleware answers "what does this
    deployment enforce?", and sharing that across Runtimes means one test's gate
    silently applies to another's run. pipeshub reached the same conclusion the
    hard way and states it plainly: nothing process-global, "so tests and
    concurrent runs never share middleware state".

    Registration is a decorator, with or without routing arguments::

        @rt.hooks.before_step
        async def trace(ctx): ...

        @rt.hooks.before_tool(effect=EffectClass.DESTRUCTIVE)
        async def confirm(ctx): ctx.ask("deletes data")

        @rt.hooks.around_step(target="jira.*")
        async def cached(ctx, next): ...
    """

    def __init__(self, owner: Any = None) -> None:
        self._owner = owner
        self._registrations: list[_Registration] = []
        # Their own lists, so neither family pays to scan the other: a body
        # hook is never a candidate for an effect call, and vice versa.
        self._body: dict[str, list[tuple[str, Any]]] = {"start": [], "end": []}
        self._agent: dict[str, list[tuple[str, Any]]] = {
            event: [] for event in _AGENT_EVENTS
        }
        self._installed = False

    def __bool__(self) -> bool:
        return bool(self._registrations) or self.has_body or self.has_agent

    def __len__(self) -> int:
        return (
            len(self._registrations)
            + sum(len(v) for v in self._body.values())
            + sum(len(v) for v in self._agent.values())
        )

    @property
    def has_agent(self) -> bool:
        """Whether any agent-family hook is registered. Read once per turn."""
        return any(self._agent.values())

    @property
    def has_body(self) -> bool:
        """Whether any body hook is registered.

        Read on every body entry, so it is a property over two lists rather
        than anything that allocates.
        """
        return bool(self._body["start"] or self._body["end"])

    def names(self) -> list[str]:
        """Every registered middleware, in order. Recorded on the run."""
        return (
            [r.label for r in self._registrations]
            + [label for hooks in self._body.values() for label, _ in hooks]
            + [label for hooks in self._agent.values() for label, _ in hooks]
        )

    # -- registration ---------------------------------------------------------

    def _register(self, event: str, shape: str, fn: F, routing: dict[str, Any]) -> F:
        label = routing.pop("label", "") or getattr(fn, "__name__", repr(fn))
        matches = _build_matcher(event, **routing)
        # `around` is already the kernel's own shape; the other two are
        # compiled onto it, which is where their guarantees come from.
        if shape == "before":
            wrap: Wrap = _from_before(fn, label)
        elif shape == "after":
            wrap = _from_after(fn, label)
        else:
            wrap = fn
        self._registrations.append(_Registration(matches, wrap, label))
        self._install()
        return fn

    def _install(self) -> None:
        """Put a :class:`HookBroker` in the chain, on first registration only.

        Lazy so that a Runtime with no hooks keeps exactly the broker chain it
        has today — the cost of this feature to everyone not using it is zero
        rather than nearly zero, which is the bar the whole seam sits at: an
        empty pipeline would otherwise run on every durable operation of every
        workflow.
        """
        if self._installed or self._owner is None:
            return
        broker = getattr(self._owner, "broker", None)
        if broker is None:
            return
        self._owner.broker = HookBroker(broker, self)
        self._installed = True

    def _shape(self, event: str, shape: str) -> Any:
        def register(fn: Callable[..., Any] | None = None, **routing: Any) -> Any:
            if fn is not None:
                return self._register(event, shape, fn, routing)

            def decorate(inner: F) -> F:
                return self._register(event, shape, inner, routing)

            return decorate

        return register

    # -- events ---------------------------------------------------------------
    #
    # Named per event so a call site reads as what it does. They are all one
    # mechanism underneath: the event is a matcher argument.

    def __getattr__(self, name: str) -> Any:
        for shape in ("before", "after", "around"):
            prefix = f"{shape}_"
            if name.startswith(prefix):
                event = name[len(prefix) :]
                if event in _KINDS or event in ("node", "any"):
                    return self._shape("*" if event == "any" else event, shape)
        if name.startswith("on_"):
            event = name[len("on_") :]
            if event in _AGENT_EVENTS:
                return self._observer(self._agent, event)
            if event in ("workflow_start", "workflow_end"):
                return self._observer(self._body, event.split("_", 1)[1])
        raise AttributeError(
            f"{name!r} is not a hook event.\n"
            f"  effect:   {{before,after,around}}_"
            f"{{{','.join((*_KINDS, 'node', 'any'))}}}\n"
            f"  body:     on_workflow_start, on_workflow_end\n"
            f"  agent:    {', '.join('on_' + e for e in _AGENT_EVENTS)}"
        )

    @staticmethod
    def _observer(store: dict[str, list[tuple[str, Any]]], event: str) -> Any:
        """Register an observer. No routing and no shapes — these families
        cannot decide, so there is nothing for a matcher to protect and no
        ``next`` for anyone to forget."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            store[event].append((getattr(fn, "__name__", repr(fn)), fn))
            return fn

        return register

    # -- body hooks -----------------------------------------------------------

    #: ``on_workflow_start`` fires before each entry into a body, re-entries
    #: included — check ``ctx.re_entry`` to tell "began" from "resumed".
    #: ``on_workflow_end`` fires after each exit, however it exited, with
    #: ``ctx.status`` naming which. Both are generated by ``__getattr__``.

    async def dispatch_body(self, when: str, ctx: BodyContext) -> None:
        """Run the body hooks for *when*, in registration order.

        **Always fails open.** A body hook cannot decide, so an exception in one
        must not be able to change what the run does — otherwise a broken logger
        becomes a failed workflow, and worse, a workflow that fails only on the
        deployments where that logger is installed.
        """
        for label, fn in self._body[when]:
            try:
                await fn(ctx)
            except Exception as exc:
                logger.exception(
                    "workflow %s hook %s failed for %s", when, label, ctx.run_id
                )
                ctx.metadata.setdefault("hook_errors", []).append(f"{label}: {exc}")

    # -- agent hooks ----------------------------------------------------------

    #: ``on_agent_start``/``on_agent_end`` bracket one agent run;
    #: ``on_turn_start``/``on_turn_end`` each turn; ``on_model_start`` is where
    #: messages are shaped and ``on_model_end`` where the response is observed.

    async def dispatch_agent(self, event: str, ctx: AgentHookContext) -> None:
        """Run the agent hooks for *event*, in registration order.

        **Always fails open**, for the same reason the body family does: these
        cannot decide, so an exception in one must not be able to change what
        the run does. A compaction middleware that throws leaves the messages as
        they were and the turn proceeds — degraded, and honestly so, rather than
        turning a working agent into a failed step.
        """
        for label, fn in self._agent.get(event, ()):
            try:
                await fn(ctx)
            except Exception as exc:
                logger.exception("agent %s hook %s failed", event, label)
                ctx.metadata.setdefault("hook_errors", []).append(f"{label}: {exc}")

    # -- adapters -------------------------------------------------------------

    def use_guardrail(self, guard: Any, **routing: Any) -> Any:
        """Register an existing :class:`~loom.agents.guardrails.Guardrail`.

        An **adapter, not a migration**, and the distinction is the point.
        ``Agent(guardrails=[...])`` keeps working exactly as it does — it has
        to, because an agent can run with no Runtime at all and therefore with
        no hook registry to fall back on. What this adds is the ability to
        apply the same check to *every* durable call rather than only to the
        tool calls of one agent, without writing it twice.

        The four actions map without loss::

            ALLOW    -> allow
            REJECT   -> deny(message)   — the model gets the explanation, as now
            TRIPWIRE -> raise           — aborts the run, as now
            REPLACE  -> result rewritten

        ``REPLACE`` is the one that needs care: a guardrail returning it is
        substituting *content*, which for a pre-call gate means substituting
        arguments. Since a guardrail written for an agent's tool call was
        written to substitute the call's input, that is what it does here.
        """
        from loom.agents.guardrails import GuardrailAction

        async def check(ctx: HookContext) -> None:
            verdict = await guard.evaluate(ctx.arguments)
            if verdict.action is GuardrailAction.TRIPWIRE:
                from loom.core.exceptions import GuardrailTripwire

                raise GuardrailTripwire(
                    verdict.message, guardrail_name=guard.name, info=verdict.info
                )
            if verdict.action is GuardrailAction.REJECT:
                ctx._escalate(Decision.DENY, verdict.message, by=guard.name)
            elif verdict.action is GuardrailAction.REPLACE and isinstance(
                verdict.replacement, dict
            ):
                ctx.arguments.clear()
                ctx.arguments.update(verdict.replacement)

        check.__name__ = f"guardrail:{guard.name}"
        event = routing.pop("event", "any")
        return self._shape("*" if event == "any" else event, "before")(
            check, **routing
        )

    # -- dispatch -------------------------------------------------------------

    def chain_for(self, call: EffectCall) -> list[_Registration]:
        """The middleware matching this call, in registration order."""
        return [r for r in self._registrations if r.matches(call)]


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------


class HookBroker:
    """Runs registered middleware around every durable operation.

    Composed **outermost** — ``HookBroker(TaintBroker(GuardedBroker(...)))`` —
    so a hook sees a call before taint and grants weigh in, and a hook's refusal
    costs nothing downstream.

    Forwards ``observe_run``/``forget_run`` to the broker it wraps. CLAUDE.md
    currently makes that a standing obligation on every author of a wrapping
    broker; this discharges it once, for everyone who uses hooks instead of
    writing one.
    """

    def __init__(self, inner: EffectBroker, registry: HookRegistry) -> None:
        self.inner = inner
        self.registry = registry

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        chain = self.registry.chain_for(call)
        if not chain:
            return await self.inner.dispatch(call, authority)

        ctx = HookContext(call)

        async def inner() -> Any:
            outcome = await self.inner.dispatch(call, authority)
            if not outcome.ok:
                # A refusal from further down the chain — a grant, taint, a
                # ceiling. Raised so `after` middleware sees it as an error
                # rather than as a value, then turned back into a result below.
                raise _Refused(outcome)
            return outcome.value

        try:
            value = await _compose([r.wrap for r in chain], ctx, inner)()
        except _Refused as refused:
            return refused.outcome

        if ctx.decision is Decision.DENY:
            return EffectResult(
                ok=False,
                error=_refusal_message(ctx),
                needs=ctx.reason,
            )
        if ctx.decision is Decision.ASK:
            return await self._ask(ctx, call, authority)
        return EffectResult(value=value)

    async def _ask(
        self, ctx: HookContext, call: EffectCall, authority: Authority
    ) -> EffectResult:
        """Park the run on a human, then proceed or refuse on their answer.

        The one thing Loom can do here that a request-scoped middleware stack
        cannot: parking costs nothing while it waits, and the answer is
        journaled. On resume the body re-enters, this call is still
        un-journaled, the hook runs again — and ``wait_for_approval`` finds the
        recorded answer instead of asking twice. The stable nested path is what
        makes that work.

        With no workflow context there is nobody to ask, so an unanswerable
        ``ask`` refuses rather than proceeding: a gate that could not run has
        not passed.
        """
        scoped = ctx.ctx
        if scoped is None:
            return EffectResult(
                ok=False,
                error=(
                    f"{_refusal_message(ctx)} — and no workflow context was "
                    "available to ask a human"
                ),
                needs=ctx.reason,
            )
        approved = await scoped.wait_for_approval(f"hook:{call.target}")
        if not approved:
            return EffectResult(ok=False, error=_refusal_message(ctx), needs=ctx.reason)
        return await self.inner.dispatch(call, authority)

    # -- RunObserver forwarding ----------------------------------------------

    def observe_run(self, run_id: str, journal: Any) -> None:
        if isinstance(self.inner, RunObserver):
            self.inner.observe_run(run_id, journal)

    def forget_run(self, run_id: str) -> None:
        if isinstance(self.inner, RunObserver):
            self.inner.forget_run(run_id)

    def __repr__(self) -> str:
        return f"<HookBroker {len(self.registry)} hooks -> {self.inner!r}>"


class _Refused(Exception):  # noqa: N818 - a refusal in flight, not a failure
    """A downstream refusal, in flight through the middleware chain."""

    def __init__(self, outcome: EffectResult) -> None:
        super().__init__(outcome.error or "denied")
        self.outcome = outcome


def _refusal_message(ctx: HookContext) -> str:
    by = f" by {ctx.refused_by}" if ctx.refused_by else ""
    reason = f": {ctx.reason}" if ctx.reason else ""
    return f"'{ctx.target}' refused{by}{reason}"
