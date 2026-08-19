"""Agent definition — the declarative specification of an LLM-powered agent.

An ``Agent`` is to the agent layer what ``StepDefinition`` is to the step layer:
a named, callable object that carries its own configuration and can be invoked
directly (for unit testing) or through the durable context (for production).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from loom.agents.executor import AgentContext, AgentExecutor, AgentSettings
from loom.agents.guardrails import Guardrail
from loom.agents.limits import DEFAULT_LIMITS, UsageLimits
from loom.agents.models import ModelProvider, ModelSettings
from loom.agents.result import AgentResult
from loom.agents.tools import Tool
from loom.core.exceptions import ConfigurationError

OutputT = TypeVar("OutputT")


class PersistenceClass(StrEnum):
    """How long an agent's conversation history survives."""

    EPHEMERAL = "ephemeral"
    """History discarded after each call. Stateless tool use."""
    SESSION = "session"
    """History persisted for the lifetime of a session key. Multi-turn chat."""
    PERSISTENT = "persistent"
    """History survives indefinitely. Knowledge worker, coding agent."""


@dataclass
class Agent(Generic[OutputT]):
    """A named, configurable, callable agent.

    Can be invoked three ways:

    1. ``await agent(input)`` — direct call, no journaling (for tests)
    2. ``await ctx.agent(agent, input)`` — durable call within a workflow
    3. ``session = agent.session(key="...")`` — open a multi-turn session
    """

    name: str
    instructions: str = ""
    """System prompt. Can reference ``{input}`` for template substitution."""
    model: ModelProvider | None = None
    executor: AgentExecutor | None = None
    """Custom executor. If None, uses BuiltInAgentRuntime."""
    tools: list[Tool] = field(default_factory=list)
    output_type: type[BaseModel] | None = None
    """Pydantic model for structured output validation."""
    guardrails: list[Guardrail] = field(default_factory=list)
    limits: UsageLimits = field(default_factory=lambda: DEFAULT_LIMITS)
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    persistence: PersistenceClass = PersistenceClass.EPHEMERAL
    handoffs: list[Agent[Any]] = field(default_factory=list)
    """Agents this one can delegate to via handoff."""
    user_interaction: Any = None
    """Optional :class:`~loom.agents.interaction.UserInteraction`.
    When set, an ``ask_user`` tool is injected into the turn loop unless the
    caller already provided one."""
    bounds: Any = None
    """Optional :class:`~loom.agents.bounds.ResultBounds`.

    ``None`` sends every tool result to the model whole, which is what shipped.
    Set it to cap what one result can contribute to the context — the full
    value still reaches the journal, and, when a spill store is configured,
    stays retrievable through ``read_spill`` / ``grep_spill``.
    """
    metadata: dict[str, Any] = field(default_factory=dict)

    async def __call__(
        self,
        input: Any,
        *,
        settings: AgentSettings | None = None,
        context: AgentContext | None = None,
    ) -> AgentResult[OutputT]:
        """Invoke the agent directly, bypassing the journal.

        Uses the configured executor if set, otherwise falls back to
        :class:`BuiltInAgentRuntime`.
        """
        executor = self._resolve_executor()
        return await executor.execute(
            input,
            tools=self.tools or None,
            output_type=self.output_type,
            settings=settings,
            context=context,
        )

    def _resolve_executor(self) -> AgentExecutor:
        """Get or create the executor for this agent."""
        if self.executor is not None:
            return self.executor
        from loom.agents.runner import BuiltInAgentRuntime

        return BuiltInAgentRuntime(agent=self)

    def session(
        self, key: str, *, store: Any | None = None
    ) -> AgentSession[OutputT]:
        """Open a multi-turn session against this agent.

        Each call on the returned object sees every prior turn::

            chat = support_agent.session(key=f"ticket-{ticket_id}")
            await chat("My order hasn't arrived")
            await chat("What was the tracking number again?")  # remembers

        Parameters
        ----------
        key:
            Conversation identifier. Reuse it to resume a conversation.
        store:
            A :class:`Session` implementation. Defaults to a process-local one,
            which is fine for a single process and loses history on restart —
            pass ``StoreBackedSession(runtime.store)`` to make it durable.
        """
        if self.persistence is PersistenceClass.EPHEMERAL:
            raise ConfigurationError(
                f"agent '{self.name}' is EPHEMERAL, so a session would not retain "
                "anything between calls. Construct it with "
                "persistence=PersistenceClass.SESSION."
            )
        from loom.agents.memory import InMemorySession

        return AgentSession(agent=self, key=key, store=store or InMemorySession())

    def with_settings(self, **overrides: Any) -> Agent[OutputT]:
        """Return a copy with overridden settings."""
        import copy

        clone = copy.copy(self)
        for key, value in overrides.items():
            if hasattr(clone, key):
                setattr(clone, key, value)
        return clone

    def __repr__(self) -> str:
        return f"<Agent {self.name} persistence={self.persistence.value}>"


@dataclass
class AgentSession(Generic[OutputT]):
    """A conversation with one agent, carrying history across calls.

    Returned by :meth:`Agent.session`. Callable like the agent itself; the
    difference is that prior turns are replayed into each run and the result is
    written back, so the agent actually remembers.
    """

    agent: Agent[OutputT]
    key: str
    store: Any

    async def __call__(
        self,
        input: Any,
        *,
        settings: AgentSettings | None = None,
    ) -> AgentResult[OutputT]:
        from loom.agents.memory import replace_history

        memory_key = f"{self.agent.name}:{self.key}"
        history = await self.store.get(memory_key)
        result = await self.agent(
            input,
            settings=settings,
            context=AgentContext(
                agent_id=self.agent.name,
                session_id=self.key,
                history=list(history),
            ),
        )
        if result.messages:
            await replace_history(self.store, memory_key, result.messages)
        return result

    async def history(self) -> list[Any]:
        """Every turn recorded so far, oldest first."""
        turns: list[Any] = await self.store.get(f"{self.agent.name}:{self.key}") or []
        return turns

    async def reset(self) -> None:
        """Forget this conversation. The agent itself is unchanged."""
        await self.store.clear(f"{self.agent.name}:{self.key}")
