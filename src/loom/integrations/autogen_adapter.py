"""AutoGen agent framework adapter.

Direction A: ``AutoGenExecutor`` wraps an AutoGen team or agent and
exposes it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_autogen_tool`` returns a tool dict that
lets an AutoGen agent invoke a LOOM workflow.
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


class AutoGenExecutor:
    """Wraps an AutoGen team or agent as an ``AgentExecutor``.

    Parameters
    ----------
    team:
        An AutoGen ``AgentChat`` team or single agent instance.
    """

    def __init__(self, team: Any) -> None:
        self._team = team

    async def execute(
        self,
        *,
        input: str,
        tools: list[Any] | None = None,
        output_type: type | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Run the AutoGen team on *input*.

        Delegates to ``team.run`` and returns the result from
        the team execution.
        """
        from autogen_agentchat.teams import (
            BaseGroupChat,
        )

        if not isinstance(self._team, BaseGroupChat):
            raise TypeError(
                "Expected an AutoGen BaseGroupChat, "
                f"got {type(self._team).__name__}"
            )

        from autogen_agentchat.base import (
            TaskResult,
        )

        result: TaskResult = await self._team.run(
            task=input
        )
        return coerce_output(result, output_type)


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = AutoGenExecutor(team=None)


# -- Direction B --------------------------------------------------------------


def workflow_as_autogen_tool(
    runtime: Any,
    workflow_id: str,
) -> dict[str, Any]:
    """Create an AutoGen-compatible tool dict for a workflow.

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
