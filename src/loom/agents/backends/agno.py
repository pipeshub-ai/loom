"""Agno agent backend for the LOOM runtime.

Configured on ``Runtime(agent_backend=AgnoBackend(...))``.
Converts LOOM Tools to Agno tools via the adapter pattern.

Example::

    from agno.models.anthropic import Claude
    from loom.agents.backends.agno import AgnoBackend

    backend = AgnoBackend(model=Claude(id="claude-sonnet-4-6"))
    rt = Runtime(store=MemoryStore(), agent_backend=backend)
"""

from __future__ import annotations

from typing import Any

from loom.agents.result import AgentResult
from loom.core.models import Usage


class AgnoBackend:
    """Uses an Agno Agent as the LOOM agent backend.

    Parameters
    ----------
    model:
        An Agno model instance (e.g. ``Claude(id="claude-sonnet-4-6")``).
    """

    supports_history = False
    """This backend does not yet seed a run from prior turns. Passing a
    session_id to ctx.agent() with it configured raises rather than silently
    starting each call from a blank conversation."""

    def __init__(self, *, model: Any) -> None:
        self._model = model

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
        history: list[Any] | None = None,
        agent_id: str = "",
        max_turns: int | None = None,
    ) -> AgentResult[Any]:
        """Build an Agno Agent with the given tools and invoke it."""
        from agno.agent import Agent as AgnoAgent

        agno_tools = _convert_tools(tools or [])

        agent = AgnoAgent(
            model=self._model,
            tools=agno_tools,
            markdown=False,
        )
        response = await agent.arun(prompt)

        output_text = ""
        if hasattr(response, "content"):
            output_text = response.content or ""
        elif isinstance(response, str):
            output_text = response

        usage = Usage()
        if hasattr(response, "metrics") and response.metrics:
            m = response.metrics
            usage.input_tokens = getattr(m, "input_tokens", 0) or 0
            usage.output_tokens = getattr(m, "output_tokens", 0) or 0

        return AgentResult(
            output=output_text,
            agent="agno",
            usage=usage,
        )


def _convert_tools(tools: list[Any]) -> list[Any]:
    """Convert LOOM Tools to Agno-compatible tools."""
    from loom.agents.tools import Tool as LoomTool

    converted = []
    for t in tools:
        if isinstance(t, LoomTool):
            converted.append(_loom_tool_to_agno(t))
        else:
            converted.append(t)
    return converted


def _loom_tool_to_agno(tool: Any) -> Any:
    """Convert a LOOM Tool to an Agno function-based tool."""
    import asyncio
    import inspect

    from agno.tools import tool as agno_tool

    fn = tool.fn
    if asyncio.iscoroutinefunction(fn):
        @agno_tool(name=tool.name, description=tool.description or tool.name)
        async def wrapper(**kwargs: Any) -> str:
            # tool.render_result, not str(): `str(Results([1,2,3],
            # complete=False, total=312))` is "[1, 2, 3]" — page one rendered
            # exactly like a complete answer, with the coverage the paging layer
            # computed thrown away on the last line before the model reads it.
            rendered: str = tool.render_result(await fn(**kwargs))
            return rendered
    else:
        @agno_tool(name=tool.name, description=tool.description or tool.name)
        def wrapper(**kwargs: Any) -> str:
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                result = asyncio.get_event_loop().run_until_complete(result)
            rendered: str = tool.render_result(result)
            return rendered

    return wrapper
