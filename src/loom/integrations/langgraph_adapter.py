"""LangGraph / LangChain agent framework adapter.

Direction A: ``LangGraphExecutor`` wraps a compiled LangGraph graph
and exposes it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_langchain_tool`` creates a tool dict that
lets LangChain agents invoke a LOOM workflow.
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


class LangGraphExecutor:
    """Wraps a compiled LangGraph graph as an ``AgentExecutor``.

    Parameters
    ----------
    graph:
        A compiled LangGraph ``CompiledGraph`` instance.
    """

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def execute(
        self,
        *,
        input: str,
        tools: list[Any] | None = None,
        output_type: type | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Invoke the LangGraph graph asynchronously.

        The graph is invoked with ``ainvoke`` using a messages
        list derived from *input*.
        """
        from langchain_core.messages import HumanMessage
        from langgraph.graph.state import CompiledStateGraph

        if not isinstance(self._graph, CompiledStateGraph):
            raise TypeError(
                "Expected a compiled LangGraph StateGraph, "
                f"got {type(self._graph).__name__}"
            )

        invoke_input: dict[str, Any] = {
            "messages": [HumanMessage(content=input)],
        }
        config: dict[str, Any] = {}
        if settings:
            config["configurable"] = settings

        result = await self._graph.ainvoke(
            invoke_input, config=config or None
        )
        return coerce_output(result, output_type)


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = LangGraphExecutor(graph=None)  # type: ignore[arg-type]


# -- Direction B --------------------------------------------------------------


def workflow_as_langchain_tool(
    runtime: Any,
    workflow_id: str,
    description: str,
) -> dict[str, Any]:
    """Create a LangChain-compatible tool dict for a workflow.

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
