"""One authoring job, as an object: a conversation the model keeps, and a
budget spanning every call the job makes.

Two defects share a cause, and the cause is that a generation was implemented
as a sequence of *unrelated* agent invocations.

**The model forgot everything between rounds.** ``WorkflowCodingAgent`` called
``await agent(spec)``, then later ``await agent(repair_prompt)`` — always
without a ``context``, so ``AgentContext.history`` was empty and the runner
rebuilt the conversation from the system prompt and the new input alone. Every
repair round therefore lost the toolset schemas the model had fetched, the
entity ids it had resolved through real API calls, and its own plan. It was
asked to fix a traceback in code it could not remember writing, against APIs it
could no longer see — and the pipeline compensated with a heuristic ("if the
new code has no fewer errors, keep the old one") that treats the symptom.

**Nothing bounded the job.** ``turn`` and ``cumulative_usage`` are locals inside
the runner's loop, so each invocation restarted at turn 1 with the full
``max_turns`` allowance. A generation with three repair rounds and two review
rounds got six independent budgets, and ``max_cost_usd`` bounded one call and
never the work. The comment claiming the repair "shares the agent's turn budget
with the generation that preceded it" was simply false.

:class:`CodingSession` makes both the same object's business: it carries the
transcript forward and charges every call against one :class:`GenerationBudget`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loom.agents.limits import UsageLimits
from loom.agents.messages import Message, Role
from loom.core.exceptions import ConfigurationError
from loom.core.models import Usage

if TYPE_CHECKING:
    from loom.agents.result import AgentResult

logger = logging.getLogger("workflow.coding_agent")

__all__ = ["Asking", "BudgetExhausted", "CodingSession", "GenerationBudget"]


@runtime_checkable
class Asking(Protocol):
    """What the repair and review loops need from whatever drives the model.

    One method, so a test can supply a three-line stand-in and the loops stay
    testable without a model, a budget, or a transcript. :class:`CodingSession`
    is the implementation the agent uses.
    """

    async def ask(self, prompt: str) -> AgentResult[Any]:
        """Continue the conversation with *prompt* and return the result."""
        ...


class BudgetExhausted(Exception):  # noqa: N818 - names the event, not an error
    """The job has spent what it was given. Raised before a call, never after.

    Not a ``WorkflowError``: this is an authoring-time budget, not something a
    running workflow can encounter.
    """


@dataclass
class GenerationBudget:
    """What one authoring job may spend, across every call it makes.

    Turns are the primary dial because they are what actually runs away: a
    model that cannot find a toolset loops on ``search_toolsets``. Tokens and
    dollars are optional ceilings on top.
    """

    max_turns: int
    """Model round trips for the whole job — discovery, repair and review
    together, not each."""
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None

    spent: Usage = field(default_factory=Usage)
    turns_used: int = 0

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ConfigurationError("a generation budget needs at least one turn")

    @classmethod
    def for_agent(
        cls,
        model_name: str,
        *,
        max_turns: int,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> GenerationBudget:
        """Build a budget, refusing a dollar ceiling that cannot be measured.

        ``estimate_cost`` returns ``0.0`` for a model with no price on file, so
        a ``max_cost_usd`` against one is a ceiling that can never be reached —
        a budget that reads as enforced and is not. Refused at construction,
        where the caller can act on it.
        """
        from loom.agents.models import is_priced

        if max_cost_usd is not None and not is_priced(model_name):
            raise ConfigurationError(
                f"max_cost_usd was set but there is no price on file for "
                f"'{model_name}', so the running cost would always read as "
                f"$0.00 and the ceiling could never be reached. Add it to "
                f"loom.agents.models.PRICING, or drop max_cost_usd and bound "
                f"the job with max_turns."
            )
        return cls(
            max_turns=max_turns,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
        )

    @property
    def turns_left(self) -> int:
        return max(0, self.max_turns - self.turns_used)

    @property
    def exhausted(self) -> bool:
        """Whether another call may be made at all."""
        return bool(self.reason_exhausted)

    @property
    def reason_exhausted(self) -> str:
        """Why no further call may be made, or ``""``."""
        if self.turns_left <= 0:
            return f"the {self.max_turns}-turn budget for this job is spent"
        if (
            self.max_total_tokens is not None
            and self.spent.total_tokens >= self.max_total_tokens
        ):
            return (
                f"{self.spent.total_tokens} tokens spent of "
                f"{self.max_total_tokens}"
            )
        if self.max_cost_usd is not None and self.spent.cost_usd >= self.max_cost_usd:
            return f"${self.spent.cost_usd:.2f} spent of ${self.max_cost_usd:.2f}"
        return ""

    def limits_for_next_call(self) -> UsageLimits:
        """The per-call ceiling that keeps one call inside the job's budget.

        Turns are what is left of the job, not what is left of a call — which
        is the whole point: without this, every invocation restarted at turn 1
        with the full allowance.
        """
        return UsageLimits(
            max_turns=max(1, self.turns_left),
            max_total_tokens=self._remaining(
                self.max_total_tokens, self.spent.total_tokens
            ),
            max_cost_usd=self._remaining(self.max_cost_usd, self.spent.cost_usd),
        )

    def charge(self, result: AgentResult[Any]) -> None:
        """Record what a completed call consumed.

        Read defensively. ``AgentExecutor`` is a seam a host implements — a
        LangGraph or Agno adapter is under no obligation to report a turn count
        or a token total, and a budget that *raised* on one that did not would
        make the seam narrower than it is documented to be. A call that reports
        nothing still costs a turn, which is the ceiling that matters.
        """
        self.turns_used += int(getattr(result, "turns", 1) or 1)
        self.spent.add(_as_usage(getattr(result, "usage", None)))

    @staticmethod
    def _remaining(ceiling: float | None, used: float) -> Any:
        if ceiling is None:
            return None
        return max(ceiling - used, 0.0) or None

    def describe(self) -> str:
        return (
            f"turns {self.turns_used}/{self.max_turns} · "
            f"tokens {self.spent.total_tokens} · ${self.spent.cost_usd:.4f}"
        )


class CodingSession:
    """One authoring job against one agent.

    Holds the transcript so a repair round can see what discovery found, and
    charges every call against one budget. The agent itself is unchanged and
    unaware — this is composition around it, not a fork of the turn loop.
    """

    def __init__(
        self, agent: Any, budget: GenerationBudget, *, hooks: Any | None = None
    ) -> None:
        self._agent = agent
        self._budget = budget
        self._hooks = hooks
        """Carried on every call rather than passed at one of them.

        The runner reads ``AgentContext.hooks`` and, when it finds a registry,
        brackets the whole run with the agent family — turns, model calls, and
        each tool call as it is dispatched. This class already constructs the
        one ``AgentContext`` a job makes, so it is where the registry has to
        arrive; the field was simply never filled, which is why an authoring
        run had no observable progress at all."""
        self._history: list[Message] = []

    @property
    def budget(self) -> GenerationBudget:
        return self._budget

    @property
    def history(self) -> list[Message]:
        """The transcript so far, system prompt excluded."""
        return list(self._history)

    async def ask(self, prompt: str) -> AgentResult[Any]:
        """Continue the conversation, inside the job's remaining budget.

        Raises :class:`BudgetExhausted` *before* spending anything, so a caller
        that is out of budget keeps whatever it has already produced rather
        than losing it to a call that could not have finished.
        """
        from loom.agents.executor import AgentContext

        if self._budget.exhausted:
            raise BudgetExhausted(self._budget.reason_exhausted)

        previous_limits = getattr(self._agent, "limits", None)
        self._agent.limits = self._budget.limits_for_next_call()
        try:
            result: AgentResult[Any] = await self._agent(
                prompt,
                context=AgentContext(
                    agent_id=getattr(self._agent, "name", ""),
                    history=list(self._history),
                    hooks=self._hooks,
                ),
            )
        except BaseException as failed:
            # A call that ran out of turns still *spent* them. The turn loop
            # attaches what it had consumed to the exception on the way out
            # (see `UsageLimitExceeded.usage`), because the accounting is a
            # local the exception unwinds past — and without charging it here,
            # a generation that burned a whole budget reports zero tokens to
            # whoever is deciding whether to raise that budget.
            spent = getattr(failed, "usage", None)
            if spent is not None:
                self._budget.spent.add(_as_usage(spent))
                self._budget.turns_used += int(getattr(failed, "turns", 0) or 0)
            raise
        finally:
            self._agent.limits = previous_limits

        self._budget.charge(result)
        self._remember(result)
        logger.info("session | %s", self._budget.describe())
        return result

    def _remember(self, result: AgentResult[Any]) -> None:
        """Carry this call's turns forward, minus the system prompt.

        The runner prepends ``agent.instructions`` itself on every call, so a
        system message kept here would be sent twice and grow by one each
        round.
        """
        # `getattr` for the same reason `charge` reads defensively: an
        # `AgentExecutor` a host supplies is not obliged to hand back a
        # transcript, and a session that *required* one would make the budget
        # the reason a third-party executor could not be used. No transcript
        # simply means the next round starts fresh, which is the behaviour
        # every round had before this class existed.
        messages = getattr(result, "messages", None) or []
        self._history = [
            message
            for message in messages
            if getattr(message, "role", None) is not Role.SYSTEM
        ]


def _as_usage(value: Any) -> Usage:
    """Whatever an executor reported, as a :class:`Usage`.

    Field-by-field rather than ``isinstance``: ``AgentExecutor`` is a seam a
    host implements, and an adapter that reports a plain object with two of the
    six counters is answering the question honestly. Requiring the exact model
    would make a budget the reason a third-party executor could not be used at
    all.
    """
    if isinstance(value, Usage):
        return value
    if value is None:
        return Usage()
    return Usage(
        requests=int(getattr(value, "requests", 0) or 0),
        input_tokens=int(getattr(value, "input_tokens", 0) or 0),
        output_tokens=int(getattr(value, "output_tokens", 0) or 0),
        cached_input_tokens=int(getattr(value, "cached_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(value, "cache_write_tokens", 0) or 0),
        reasoning_tokens=int(getattr(value, "reasoning_tokens", 0) or 0),
        cost_usd=float(getattr(value, "cost_usd", 0.0) or 0.0),
    )
