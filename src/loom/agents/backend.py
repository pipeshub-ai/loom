"""Agent backend protocol — the pluggable execution layer for ctx.agent().

The ``AgentBackend`` is configured once on the ``Runtime``. When workflow
code calls ``ctx.agent("prompt")``, the runtime resolves tools from the
``ToolsetRegistry`` and passes them to ``backend.run(prompt, tools=...)``.

The workflow code never imports or references any agent framework.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loom.agents.messages import Message
from loom.agents.result import AgentResult


@runtime_checkable
class AgentBackend(Protocol):
    """Pluggable agent execution backend.

    Implementations own the turn loop, tool dispatch, and model calls.
    The workflow engine only sees ``AgentResult`` at the boundary.

    The ``tools`` parameter carries LOOM ``Tool`` objects resolved from
    the ``ToolsetRegistry``. Each backend converts them to its native
    format (one adapter function per framework).
    """

    supports_history: bool
    """Whether this backend seeds a run from prior turns.

    Declared rather than assumed: a backend that quietly ignores ``history``
    would turn a multi-turn conversation into a series of amnesiac one-shots
    that still *look* like a conversation from the caller's side.
    """

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
        history: list[Message] | None = None,
        agent_id: str = "",
        max_turns: int | None = None,
    ) -> AgentResult[Any]:
        """Execute the agent with the given prompt and tools.

        Parameters
        ----------
        prompt:
            The user's request.
        tools:
            LOOM ``Tool`` objects from the registry. The backend
            converts these to framework-native tools internally.
        history:
            Prior turns of this conversation, oldest first. Only meaningful
            when ``supports_history`` is ``True``.
        agent_id:
            Stable identity of the agent being invoked. Backends may use it to
            name the run; the runtime uses it to key stored memory.
        max_turns:
            Per-call override of the backend's turn budget.

        Returns
        -------
        AgentResult
            ``messages`` should hold the full conversation — history included —
            so the runtime can persist it as the session's new state.
        """
        ...


class BuiltInBackend:
    """Default backend using the LOOM BuiltInAgentRuntime.

    Parameters
    ----------
    model:
        A ``ModelProvider`` instance (e.g. ``AnthropicProvider()``).
    instructions:
        Optional system prompt prepended to every agent call.
    clock:
        Optional :class:`~loom.runtime.clock.Clock`, threaded through to the
        current-date block the turn loop puts in the system prompt. Left out,
        that block reads the wall clock — right in production, and wrong for a
        test that has moved the Runtime's clock and expects the agent to agree
        with the workflow calling it.
    """

    supports_history = True

    def __init__(
        self,
        model: Any,
        *,
        instructions: str = "",
        clock: Any | None = None,
    ) -> None:
        self._model = model
        self._instructions = instructions
        self._clock = clock

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
        history: list[Message] | None = None,
        agent_id: str = "",
        max_turns: int | None = None,
    ) -> AgentResult[Any]:
        """Run the built-in agent turn loop with the given tools."""
        from loom.agents.agent import Agent
        from loom.agents.executor import AgentContext, AgentSettings

        agent: Agent[Any] = Agent(
            name=agent_id or "builtin",
            instructions=self._instructions,
            model=self._model,
            tools=tools or [],
        )
        return await agent(
            prompt,
            settings=AgentSettings(max_turns=max_turns) if max_turns else None,
            context=AgentContext(
                agent_id=agent_id,
                history=list(history or []),
                clock=self._clock,
            ),
        )
