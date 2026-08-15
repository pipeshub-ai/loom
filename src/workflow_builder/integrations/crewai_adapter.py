"""CrewAI agent framework adapter.

Direction A: ``CrewAIExecutor`` wraps a CrewAI ``Crew`` and exposes
it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_crew_tool`` returns a tool dict that lets
a CrewAI agent invoke a LOOM workflow.
"""

from __future__ import annotations

from typing import Any

from workflow_builder.integrations.base import AgentExecutor
from workflow_builder.integrations.structured import coerce_output


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


class CrewAIExecutor:
    """Wraps a CrewAI ``Crew`` as an ``AgentExecutor``.

    Parameters
    ----------
    crew:
        A ``crewai.Crew`` instance.
    """

    def __init__(self, crew: Any) -> None:
        self._crew = crew

    async def execute(
        self,
        *,
        input: str,
        tools: list[Any] | None = None,
        output_type: type | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Kick off the CrewAI crew on *input*.

        Delegates to ``crew.kickoff_async`` and returns the raw
        result from the crew run.
        """
        from crewai import Crew

        if not isinstance(self._crew, Crew):
            raise TypeError(
                "Expected a crewai.Crew, "
                f"got {type(self._crew).__name__}"
            )

        result = await self._crew.kickoff_async(
            inputs={"input": input}
        )
        return coerce_output(result, output_type)


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = CrewAIExecutor(crew=None)  # type: ignore[arg-type]


# -- Direction B --------------------------------------------------------------


def workflow_as_crew_tool(
    runtime: Any,
    workflow_id: str,
    description: str,
) -> dict[str, Any]:
    """Create a CrewAI-compatible tool dict for a workflow.

    Parameters
    ----------
    runtime:
        A ``workflow_builder.Runtime`` instance.
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
