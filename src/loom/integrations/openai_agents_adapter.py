"""OpenAI Agents SDK adapter.

Direction A: ``OpenAIAgentsExecutor`` wraps an OpenAI Agents SDK
``Agent`` and exposes it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_openai_tool`` returns a tool dict that
lets an OpenAI agent invoke a LOOM workflow.
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


class OpenAIAgentsExecutor:
    """Wraps an OpenAI Agents SDK ``Agent`` as an ``AgentExecutor``.

    Parameters
    ----------
    agent:
        An ``agents.Agent`` instance from the OpenAI Agents SDK.
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
        """Run the OpenAI Agents SDK agent on *input*.

        Delegates to ``Runner.run`` and returns the final
        output from the result.
        """
        from agents import Agent, Runner

        if not isinstance(self._agent, Agent):
            raise TypeError(
                "Expected an agents.Agent, "
                f"got {type(self._agent).__name__}"
            )

        result = await Runner.run(
            self._agent, input=input
        )
        return coerce_output(result.final_output, output_type)


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = OpenAIAgentsExecutor(agent=None)  # type: ignore[arg-type]


# -- Direction B --------------------------------------------------------------


def workflow_as_openai_tool(
    runtime: Any,
    workflow_id: str,
    description: str,
) -> dict[str, Any]:
    """Create an OpenAI-compatible tool dict for a workflow.

    Parameters
    ----------
    runtime:
        A ``loom.Runtime`` instance.
    workflow_id:
        The registered workflow name.
    description:
        Human-readable description shown to the LLM.

    Returns
    -------
    dict
        A tool-schema dict with ``name``, ``description``, and
        ``input_schema`` keys.
    """
    return _build_tool_schema(workflow_id, description)
