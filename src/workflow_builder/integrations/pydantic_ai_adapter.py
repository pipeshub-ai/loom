"""Pydantic AI agent framework adapter.

Direction A: ``PydanticAIExecutor`` wraps a pydantic-ai ``Agent``
and exposes it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_pydantic_tool`` returns an async callable
that lets a pydantic-ai agent invoke a LOOM workflow.
"""

from __future__ import annotations

from typing import Any

from workflow_builder.integrations.base import AgentExecutor


class PydanticAIExecutor:
    """Wraps a pydantic-ai ``Agent`` as an ``AgentExecutor``.

    Parameters
    ----------
    agent:
        A ``pydantic_ai.Agent`` instance.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def execute(
        self,
        *,
        input: str,
        tools: list[Any] | None = None,
        output_type: type | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Run the pydantic-ai agent on *input*.

        Delegates to ``agent.run`` and returns the ``data``
        attribute of the result.
        """
        from pydantic_ai import Agent

        if not isinstance(self._agent, Agent):
            raise TypeError(
                "Expected a pydantic_ai.Agent, "
                f"got {type(self._agent).__name__}"
            )

        result = await self._agent.run(input)
        return result.data


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = PydanticAIExecutor(agent=None)  # type: ignore[arg-type]


# -- Direction B --------------------------------------------------------------


def workflow_as_pydantic_tool(
    runtime: Any,
    workflow_id: str,
    description: str = "",
) -> dict[str, Any]:
    """Return a tool schema dict for a LOOM workflow.

    Parameters
    ----------
    runtime:
        A ``workflow_builder.Runtime`` instance.
    workflow_id:
        The registered workflow name.
    description:
        Human-readable description of the workflow.
    """
    return {
        "name": f"loom_{workflow_id}",
        "description": (
            description
            or f"Run the '{workflow_id}' LOOM workflow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "input_data": {"type": "object"},
            },
        },
    }
