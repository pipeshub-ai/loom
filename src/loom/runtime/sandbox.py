"""Where a workflow body runs, and what it can reach while it runs there.

Generated code executes in-process by default. That is the right default for a
developer and the wrong one for a host running code a model wrote against
credentials the host holds: the body can read ``os.environ``, open sockets, and
import anything.

**This is not a ``DurabilityBackend``.** That port answers *where durability
lives* — embedded, Temporal, DBOS. Process isolation is orthogonal: you want a
sandbox on the embedded backend, and Temporal already has its own workers.
Conflating them means a host cannot have both.

The seam is one method, and it sits where the engine invokes the body::

    Runtime(sandbox=SubprocessSandbox(SandboxPolicy(allowed_env={"TZ"})))

Everything durable still goes through the broker chain. A sandboxed body's
``ctx.*`` calls arrive back over a :class:`ContextChannel` speaking the same
``EffectCall``/``EffectResult`` the broker already speaks — so grants, budgets,
dry-run, and taint apply identically in both modes. A second enforcement path
would be a second thing to get wrong.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loom.core.exceptions import WorkflowError
from loom.runtime.effects import EffectCall, EffectResult

__all__ = [
    "BrokerChannel",
    "ContextChannel",
    "ExecutionSandbox",
    "InlineSandbox",
    "RuntimeChannel",
    "SandboxBody",
    "SandboxOutcome",
    "SandboxPolicy",
    "SandboxViolation",
]


class SandboxViolation(WorkflowError):  # noqa: N818 - names the event
    """A body exceeded what its policy allowed.

    Distinct from a workflow failure: the code did not go wrong, it was stopped.
    A host wanting to tell "this workflow is broken" from "this workflow tried
    something it may not" needs the two to be different exceptions.
    """


@dataclass(frozen=True)
class SandboxPolicy:
    """What a body may reach. The host decides; the sandbox enforces.

    Every limit defaults to the *restrictive* value, because a policy object
    exists precisely to be restrictive — a caller who wanted no limits would not
    have constructed one. The exception is ``max_wall_seconds``, which is a
    timeout rather than a permission and has to be finite for the sandbox to be
    able to give up at all.
    """

    allowed_env: frozenset[str] = frozenset()
    """Environment variables passed through. Everything else is stripped.

    An allowlist, not a denylist: a denylist has to enumerate every secret name
    anybody will ever use, and gets it wrong once."""

    allowed_imports: frozenset[str] | None = None
    """Top-level modules the body may import. ``None`` means no import policy —
    the check belongs to ``CodeValidator`` at authoring time and this is a
    second line, not the first."""

    max_memory_mb: int | None = None
    max_cpu_seconds: int | None = None
    max_wall_seconds: float = 300.0
    network: bool = False
    """Declared rather than enforced by this class. A sandbox that cannot
    actually prevent egress must say so through :attr:`ExecutionSandbox.enforces`
    rather than accept the flag and ignore it."""


@dataclass
class SandboxBody:
    """The workflow body, described so that *either* adapter can run it.

    Two representations of one thing, because the process boundary decides which
    is usable: in-process wants the callable that is already bound to a
    :class:`~loom.runtime.context.Context`, and a child process cannot receive a
    function object at all — pickling one would carry its closure, which is the
    trusted state that must not cross.

    Carrying both on one object is what keeps the port single-typed. The
    alternative — each adapter accepting its own body shape — means the engine
    has to know which sandbox it is talking to in order to call it, and a seam
    the caller must special-case is not a seam.
    """

    invoke: Callable[[], Awaitable[Any]]
    """Run the body here, against the live Context. What ``InlineSandbox`` uses."""

    source: str = ""
    """The body's source text, for an adapter that runs it elsewhere. Empty when
    the engine could not recover it — a sandbox needing it should say so rather
    than run something else."""

    entrypoint: str = ""
    """The name to call within :attr:`source`."""

    namespace: dict[str, Any] = field(default_factory=dict)
    """Extra bindings seeded into the child's exec namespace before its source
    runs, alongside whatever the source itself defines. Values must be plain,
    wire-safe data (typically strings) — this crosses to a subprocess as JSON,
    not a Python object, so it cannot carry a callable or closure across the
    boundary. Its purpose is a *sentinel*: a host whose generated code
    references a step by bare name (because the import that would otherwise
    bind it has been stripped for sandboxing) pre-binds that name to itself,
    so ``Ctx._named(sentinel)`` recovers the same name the parent's
    ``sandbox_steps`` map already keys on."""


@dataclass
class SandboxOutcome:
    """What came back from a body that ran somewhere else."""

    ok: bool = True
    value: Any = None
    error: str = ""
    violation: str = ""
    """Which limit fired, when one did. Empty for an ordinary failure."""
    calls: int = 0
    """Durable operations proxied back. Useful for asserting that a sandboxed
    run and an inline run did the same work."""


@runtime_checkable
class ContextChannel(Protocol):
    """The only way out of a sandbox.

    Deliberately ``EffectCall``/``EffectResult``: those are what the broker
    chain already speaks, so a sandboxed run passes through the same grants,
    budgets, dry-run, and taint as an inline one rather than a parallel copy.
    """

    async def dispatch(self, call: EffectCall) -> EffectResult: ...


@runtime_checkable
class ExecutionSandbox(Protocol):
    """Runs a workflow body, and proxies its durable calls back."""

    @property
    def name(self) -> str: ...

    @property
    def enforces(self) -> frozenset[str]:
        """Which :class:`SandboxPolicy` fields this sandbox actually enforces.

        Declared rather than assumed. A sandbox that accepts ``max_memory_mb``
        and cannot apply it would leave a host believing it has a limit it does
        not — the same failure as an adapter accepting ``output_type`` and
        returning prose.
        """
        ...

    async def run(
        self,
        *,
        body: SandboxBody,
        run_id: str,
        input: Any,
        channel: ContextChannel,
        policy: SandboxPolicy,
    ) -> SandboxOutcome:
        """Run *body*, proxying its durable calls to *channel*.

        Control flow is **not** an outcome. A body that parks raises ``Suspend``
        and a cancelled one raises ``WorkflowCancelled``; both must propagate out
        of this method untouched, because the engine distinguishes parked from
        failed and a sandbox that flattened the two would turn every human
        approval into a failure.
        """
        ...


class InlineSandbox:
    """Runs the body in this process. The default, and free.

    Exists so that going through a sandbox is not a feature you opt into: the
    seam is always there, and a deployment needing no isolation pays one
    attribute lookup for it. Anything more expensive would push people to
    bypass the seam, which defeats its purpose.

    It enforces **nothing**, and says so. A host that wants isolation picks a
    sandbox that provides it; one that reads ``enforces`` and finds it empty
    knows exactly what it has.
    """

    name = "inline"
    enforces: frozenset[str] = frozenset()

    async def run(
        self,
        *,
        body: SandboxBody,
        run_id: str,
        input: Any,
        channel: ContextChannel,
        policy: SandboxPolicy,
    ) -> SandboxOutcome:
        """Awaits the body here, and lets everything it raises through.

        No ``try`` — deliberately. The engine's own handlers distinguish parking
        from cancellation from failure, and catching here to repackage as
        ``SandboxOutcome(ok=False)`` would erase that distinction on the path
        that every default Runtime takes.
        """
        return SandboxOutcome(ok=True, value=await body.invoke())


@dataclass
class BrokerChannel:
    """A :class:`ContextChannel` straight onto a broker, with no journal.

    For sandboxing something that is *not* a run — the coding agent's smoke
    stage, a rehearsal — where there is no ``Context`` to journal against. The
    call still passes grants, budgets, and dry-run, so "sandboxed" never means
    "unmediated"; it simply is not replayable, because nothing recorded it.

    A real run wants :class:`RuntimeChannel`. The distinction is visible in the
    type rather than in a flag, so choosing wrong is a wiring error rather than
    a run that turns out, later, to have journaled nothing.
    """

    broker: Any
    authority: Any
    seen: list[EffectCall] = field(default_factory=list)

    async def dispatch(self, call: EffectCall) -> EffectResult:
        self.seen.append(call)
        result: EffectResult = await self.broker.dispatch(call, self.authority)
        return result


@dataclass
class RuntimeChannel:
    """A :class:`ContextChannel` onto the parent's live :class:`Context`.

    This is what makes a sandboxed run and an inline one produce **the same
    journal**: a proxied call is turned back into the ordinary ``ctx.step(...)``
    it would have been, so it allocates the same path, journals the same entry,
    and reaches the broker by the same route. Nothing here dispatches to the
    broker itself — going around ``Context`` to reach it would produce exactly
    the second enforcement path this design exists to avoid, and the calls would
    be absent from the journal that has to serve them on the next re-entry.

    The child therefore holds no store, no credentials, and no journal: it
    decides *what* to call, and the parent decides whether, performs it, and
    records it. That split is the whole security value — untrusted orchestration
    over trusted effects.

    An unresolvable target comes back as a refusal rather than an exception, so
    a body naming a step that does not exist fails like a denied effect instead
    of crashing the conversation with the child.
    """

    ctx: Any
    steps: Mapping[str, Any] = field(default_factory=dict)
    """Name → ``@step`` definition. What the sandboxed body is allowed to call:
    a body can only reach effects the host put in this map, which is a second
    reason it is built by the engine from the workflow's own module rather than
    from anything the body says."""
    seen: list[EffectCall] = field(default_factory=list)

    async def dispatch(self, call: EffectCall) -> EffectResult:
        self.seen.append(call)

        if call.kind in ("step", "tool"):
            target = self.steps.get(call.target)
            if target is None:
                return EffectResult(
                    ok=False,
                    error=(
                        f"no step named '{call.target}' is available to this "
                        f"sandbox. Available: {', '.join(sorted(self.steps)) or 'none'}"
                    ),
                )
            return EffectResult(
                value=await self.ctx.step(target, name=call.name, **call.arguments)
            )

        if call.kind == "agent":
            # `toolsets=`/`grants=` name *which capabilities* the call may
            # reach; honouring them from the child would let untrusted body
            # code widen its own toolset access past whatever the workflow's
            # own `@workflow(grants=...)` declared, on the trusted side of the
            # boundary this proxy exists to hold. Only the parent's
            # already-configured defaults apply inside a sandbox.
            arguments = {
                k: v
                for k, v in call.arguments.items()
                if k not in ("toolsets", "grants")
            }
            return EffectResult(value=await self.ctx.agent(call.target, **arguments))

        if call.kind == "node":
            arguments = dict(call.arguments)
            payload = arguments.pop("payload", None)
            return EffectResult(
                value=await self.ctx.node(call.target, payload, **arguments)
            )

        if call.kind == "event":
            # Parks the run. The Suspend raised in here propagates out through
            # the sandbox untouched — a body waiting on a person is neither
            # finished nor failed, and flattening it into either is how an
            # approval turns into an outage.
            if call.target.startswith("approval:"):
                subject = call.target.removeprefix("approval:")
                return EffectResult(
                    value=await self.ctx.wait_for_approval(subject, **call.arguments)
                )
            return EffectResult(
                value=await self.ctx.wait_for_event(call.target, **call.arguments)
            )

        if call.kind == "sleep":
            # Also parks: the Suspend raised by `ctx.sleep` propagates out
            # exactly like an event wait, and for the same reason — the child
            # holds no durable state, so re-entry replays the wake from the
            # journal rather than resuming a suspended child process.
            arguments = dict(call.arguments)
            seconds = arguments.pop("seconds", None)
            return EffectResult(value=await self.ctx.sleep(seconds, **arguments))

        if call.kind == "report":
            # Not journaled, same as an inline `ctx.report` — this reaches the
            # run's stream and nothing else, so there is nothing here to make
            # replay-safe.
            return EffectResult(
                value=await self.ctx.report(call.target, **call.arguments)
            )

        return EffectResult(
            ok=False,
            error=(
                f"a sandboxed body cannot perform '{call.kind}' operations "
                f"(asked for '{call.target}')"
            ),
        )
