"""Pydantic AI agent backend for the LOOM runtime.

Configured on ``Runtime(agent_backend=PydanticAIBackend(...))``.
Converts LOOM Tools to Pydantic AI tools via the adapter pattern.

Example::

    from loom.agents.backends.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(model="anthropic:claude-sonnet-4-6")
    rt = Runtime(store=MemoryStore(), agent_backend=backend)
"""

from __future__ import annotations

from typing import Any

from loom.agents.result import AgentResult
from loom.core.models import Usage


class PydanticAIBackend:
    """Uses a Pydantic AI Agent as the LOOM agent backend.

    Parameters
    ----------
    model:
        A Pydantic AI model string (e.g. ``"anthropic:claude-sonnet-4-6"``)
        or a model instance.
    instructions:
        Optional system prompt.
    """

    supports_history = False
    """This backend does not yet seed a run from prior turns. Passing a
    session_id to ctx.agent() with it configured raises rather than silently
    starting each call from a blank conversation."""

    def __init__(
        self,
        *,
        model: str | Any = "anthropic:claude-sonnet-4-6",
        instructions: str = "",
    ) -> None:
        self._model = model
        self._instructions = instructions

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
        history: list[Any] | None = None,
        agent_id: str = "",
        max_turns: int | None = None,
    ) -> AgentResult[Any]:
        """Build a Pydantic AI Agent with the given tools and invoke it."""
        from pydantic_ai import Agent as PAIAgent

        pai_tools = _convert_tools(tools or [])

        agent = PAIAgent(
            self._model,
            instructions=self._instructions or None,
            tools=pai_tools,
        )
        result = await agent.run(prompt)

        output_text = ""
        if hasattr(result, "output"):
            output_text = str(result.output)
        elif hasattr(result, "data"):
            output_text = str(result.data)

        usage = Usage()
        if hasattr(result, "usage") and result.usage:
            u = result.usage
            usage.input_tokens = getattr(u, "request_tokens", 0) or 0
            usage.output_tokens = getattr(u, "response_tokens", 0) or 0

        return AgentResult(
            output=output_text,
            agent="pydantic_ai",
            usage=usage,
        )


def _convert_tools(tools: list[Any]) -> list[Any]:
    """Convert LOOM Tools to Pydantic AI tools."""
    from loom.agents.tools import Tool as LoomTool

    converted = []
    for t in tools:
        if isinstance(t, LoomTool):
            converted.append(_loom_tool_to_pai(t))
        else:
            converted.append(t)
    return converted


def _loom_tool_to_pai(tool: Any) -> Any:
    """Convert a LOOM Tool to a Pydantic AI tool."""
    import asyncio
    import inspect

    from pydantic_ai import RunContext
    from pydantic_ai.tools import Tool as PAITool

    fn = tool.fn

    # Use get_type_hints-compatible annotation
    if asyncio.iscoroutinefunction(fn):
        async def wrapper(ctx, **kwargs: Any) -> str:
            result = await fn(**kwargs)
            return str(result)
    else:
        async def wrapper(ctx, **kwargs: Any) -> str:
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                return str(await result)
            return str(result)

    # Annotate ctx with RunContext for Pydantic AI schema generation
    wrapper.__annotations__["ctx"] = RunContext[Any]

    wrapper.__name__ = tool.name
    wrapper.__doc__ = tool.description or tool.name

    return PAITool(
        wrapper,
        name=tool.name,
        description=tool.description or tool.name,
        takes_ctx=True,
    )
