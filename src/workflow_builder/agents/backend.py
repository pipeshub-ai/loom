"""Agent backend protocol — the pluggable execution layer for ctx.agent().

The ``AgentBackend`` is configured once on the ``Runtime``. When workflow
code calls ``ctx.agent("prompt")``, the runtime resolves tools from the
``ToolsetRegistry`` and passes them to ``backend.run(prompt, tools=...)``.

The workflow code never imports or references any agent framework.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from workflow_builder.agents.result import AgentResult


@runtime_checkable
class AgentBackend(Protocol):
    """Pluggable agent execution backend.

    Implementations own the turn loop, tool dispatch, and model calls.
    The workflow engine only sees ``AgentResult`` at the boundary.

    The ``tools`` parameter carries LOOM ``Tool`` objects resolved from
    the ``ToolsetRegistry``. Each backend converts them to its native
    format (one adapter function per framework).
    """

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
    ) -> AgentResult[Any]:
        """Execute the agent with the given prompt and tools.

        Parameters
        ----------
        prompt:
            The user's request.
        tools:
            LOOM ``Tool`` objects from the registry. The backend
            converts these to framework-native tools internally.
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
    """

    def __init__(
        self,
        model: Any,
        *,
        instructions: str = "",
    ) -> None:
        self._model = model
        self._instructions = instructions

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
    ) -> AgentResult[Any]:
        """Run the built-in agent turn loop with the given tools."""
        from workflow_builder.agents.agent import Agent

        agent = Agent(
            name="builtin",
            instructions=self._instructions,
            model=self._model,
            tools=tools or [],
        )
        return await agent(prompt)
