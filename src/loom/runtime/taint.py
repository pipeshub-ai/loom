"""Read-to-write taint: what generated code may do after it has read the world.

The rule is one line and it is not specific to any platform: **once a run has
read data it did not bring with it, a destructive call needs a human.** A
workflow that searches the web and then deletes tickets has taken instructions
from something nobody reviewed, and the deletion is where that stops being
theoretical. It is exactly the property you want when the body was written by a
model rather than a person.

Implemented as a decorator on the existing broker chain rather than as a new
concept in the engine, so it composes with everything else and costs nothing
when absent::

    Runtime(broker=TaintBroker(GuardedBroker()))

Taint sits on the outside: it decides *whether* to dispatch, so it must be above
whatever performs the effect.

Two things are deliberate. The taint state is **per run**, because a run is the
unit a workflow author reasons about. And a verdict is **journaled** by the same
mechanism every other effect uses — recomputing it on replay against a policy
that has since changed would make a replay disagree with what actually
happened, which is the rule guard verdicts already follow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from loom.runtime.effects import EffectBroker, EffectCall, EffectResult
from loom.security.authority import Authority
from loom.toolsets.manifest import EffectClass

logger = logging.getLogger(__name__)

__all__ = ["TaintBroker", "TaintPolicy", "TaintState"]


@dataclass(frozen=True)
class TaintPolicy:
    """When a read taints, and what a taint forbids."""

    block_writes: bool = True
    """Refuse ``EffectClass.WRITE`` once tainted."""

    block_destructive: bool = True
    """Refuse ``EffectClass.DESTRUCTIVE`` once tainted.

    Separate from ``block_writes`` because the two have very different false
    positive rates: almost every useful workflow writes something after reading,
    while very few need to delete. A deployment that finds the write rule too
    strict can keep the destructive one, which is the part that cannot be
    undone."""

    approval_clears: bool = True
    """A human approval since the last read clears the taint.

    The escape hatch that keeps the rule usable: the answer to "this workflow
    legitimately needs to write after reading" is a person saying so, not
    turning the check off."""

    exempt: frozenset[str] = frozenset()
    """Targets the rule does not apply to, by ``kind:target`` or ``target``.

    For the writes a workflow makes *about itself* — progress reports, its own
    artifacts — which are not the exfiltration path this guards."""

    def exempts(self, call: EffectCall) -> bool:
        return bool(
            self.exempt
            and ({call.target, f"{call.kind}:{call.target}"} & self.exempt)
        )


@dataclass
class TaintState:
    """What one run has read, and whether a human has since signed off."""

    tainted: bool = False
    sources: list[str] = field(default_factory=list)
    """What tainted it, in order. The message names them: "you read X, then
    tried to delete Y" is actionable and "denied" is not."""

    def taint(self, source: str) -> None:
        self.tainted = True
        if source not in self.sources:
            self.sources.append(source)

    def clear(self) -> None:
        self.tainted = False
        self.sources.clear()


class TaintBroker:
    """Wraps another :class:`EffectBroker` with read-to-write taint tracking.

    Refusals come back as ``EffectResult(ok=False)`` rather than exceptions,
    matching how every other broker refuses — so a caller decides whether that
    surfaces as a step failure, a message back to an agent, or something the
    workflow handles itself.
    """

    def __init__(
        self, inner: EffectBroker, policy: TaintPolicy | None = None
    ) -> None:
        self._inner = inner
        self.policy = policy or TaintPolicy()
        self._runs: dict[str, TaintState] = {}

    def state_for(self, run_id: str) -> TaintState:
        """The taint state of one run. Public so a host can inspect or reset it."""
        return self._runs.setdefault(run_id, TaintState())

    def forget_run(self, run_id: str) -> None:
        """Drop a finished run's state, so the map does not grow forever."""
        self._runs.pop(run_id, None)

    def observe_run(self, run_id: str, journal: Any) -> None:
        """Rebuild this run's taint from its journal, before the body re-enters.

        In-memory state is not enough, and the way it fails is the worst way:
        **open**. The engine re-enters a body from the top, and every call the
        journal already has an answer for is served from it without reaching a
        broker — so a run that read the world, parked on an approval, and came
        back would arrive with nothing recorded as read, and the write the rule
        exists to stop would sail through. On another node, after a restart, or
        simply after a retry, same result.

        Deriving the state from the journal fixes that and one other thing at
        once: an *approval* is a journal entry too, and events never reach a
        broker at all — ``wait_for_event`` writes its entry directly. Read from
        memory, the escape hatch could never fire; read from the journal, it
        fires exactly when a human answered.

        Order is preserved because the journal preserves it. A read after an
        approval taints again, which is the whole point of the rule.
        """
        from loom.runtime.journal import EntryKind, EntryStatus

        state = TaintState()
        for entry in journal.entries():
            if entry.status is not EntryStatus.COMPLETED:
                continue
            if entry.kind is EntryKind.EVENT and entry.name.startswith("approval:"):
                if self.policy.approval_clears:
                    state.clear()
                continue
            effect = (entry.metadata or {}).get("effect_class")
            if effect is None or EffectClass(effect) is not EffectClass.READ:
                continue
            call = EffectCall(kind=str(entry.kind), target=entry.name)
            if not self.policy.exempts(call):
                state.taint(f"{entry.kind}:{entry.name}")
        self._runs[run_id] = state

    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult:
        state = self.state_for(call.run_id)

        clears = self._clears(call) and self.policy.approval_clears
        if clears:
            # A human said yes. Anything read before that has been reviewed, so
            # the run starts clean from here rather than staying blocked
            # forever on something that was already approved.
            state.clear()

        refusal = self._refusal(call, state)
        if refusal is not None:
            logger.info(
                "taint refused %s %s on run %s (read: %s)",
                call.effect.value,
                call.target,
                call.run_id,
                ", ".join(state.sources),
            )
            return refusal

        outcome = await self._inner.dispatch(call, authority)

        # Taint *after* the call, and only when it succeeded: a read that was
        # refused brought nothing in, and treating it as a taint would let a
        # denied call restrict everything downstream of it.
        #
        # An approval is excluded explicitly. It arrives as an event, which is a
        # read — so without this it cleared the taint on the way in and set it
        # again on the way out, and approving something changed nothing.
        if (
            outcome.ok
            and call.effect is EffectClass.READ
            and not clears
            and not self.policy.exempts(call)
        ):
            state.taint(f"{call.kind}:{call.target}")
        return outcome

    # -- decisions ----------------------------------------------------------

    @staticmethod
    def _clears(call: EffectCall) -> bool:
        """Whether this call is a human signing off.

        An approval arrives as an event named ``approval:<subject>``, which is
        the same wire ``ctx.wait_for_approval`` and every ``human.*`` node use —
        so a workflow does not have to do anything special to clear taint
        beyond asking somebody.
        """
        return call.kind == "event" and call.target.startswith("approval:")

    def _refusal(self, call: EffectCall, state: TaintState) -> EffectResult | None:
        if not state.tainted or self.policy.exempts(call):
            return None
        blocked = {
            EffectClass.WRITE: self.policy.block_writes,
            EffectClass.DESTRUCTIVE: self.policy.block_destructive,
        }.get(call.effect, False)
        if not blocked:
            return None
        return EffectResult(
            ok=False,
            error=(
                f"{call.target} is {call.effect.value} and this run has read "
                f"external data ({', '.join(state.sources)}). Ask for approval "
                "first — ctx.wait_for_approval(...) clears the taint — or mark "
                "the operation exempt if it cannot leak what was read."
            ),
            needs="approval",
        )

    def __repr__(self) -> str:
        return f"<TaintBroker over {type(self._inner).__name__}>"
