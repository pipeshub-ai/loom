"""Agno agent framework adapter.

Direction A: ``AgnoExecutor`` wraps an Agno ``Agent`` and exposes
it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_agno_tool`` returns a tool dict that
lets an Agno agent invoke a LOOM workflow.
"""

from __future__ import annotations

from typing import Any

from loom.integrations.base import AgentExecutor
from loom.integrations.structured import coerce_output


def _build_tool_schema(
    workflow_id: str, description: str
) -> dict[str, Any]:
    """Return a plain tool-schema dict for a LOOM workflow."""
    return {
        "name": f"loom_{workflow_id}",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "input_data": {"type": "object"},
            },
        },
    }


class AgnoExecutor:
    """Wraps an Agno ``Agent`` as an ``AgentExecutor``.

    Parameters
    ----------
    agent:
        An ``agno.Agent`` instance.
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
        """Run the Agno agent on *input*.

        Delegates to ``agent.arun`` and returns the response
        content.
        """
        from agno.agent import Agent

        if not isinstance(self._agent, Agent):
            raise TypeError(
                "Expected an agno.Agent, "
                f"got {type(self._agent).__name__}"
            )

        response = await self._agent.arun(input)
        return coerce_output(response.content, output_type)


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = AgnoExecutor(agent=None)


# -- Direction B --------------------------------------------------------------


def workflow_as_agno_tool(
    runtime: Any,
    workflow_id: str,
) -> dict[str, Any]:
    """Create an Agno-compatible tool dict for a workflow.

    Parameters
    ----------
    runtime:
        A ``loom.Runtime`` instance.
    workflow_id:
        The registered workflow name.

    Returns
    -------
    dict
        A tool-schema dict with ``name``, ``description``, and
        ``input_schema`` keys.
    """
    return _build_tool_schema(
        workflow_id,
        f"Run the '{workflow_id}' LOOM workflow.",
    )
