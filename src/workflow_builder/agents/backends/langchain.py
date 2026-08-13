"""LangChain agent backend for the LOOM runtime.

Configured once on ``Runtime(agent_backend=LangChainBackend(llm=...))``.
When ``ctx.agent("prompt")`` is called, the runtime resolves LOOM tools
from the registry and passes them here. This backend converts them to
LangChain-native tools, builds a ReAct agent, and invokes it.

The workflow code never imports LangChain.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from workflow_builder.agents.result import AgentResult
from workflow_builder.core.models import Usage


class LangChainBackend:
    """Uses a LangGraph ReAct agent as the LOOM agent backend.

    Parameters
    ----------
    llm:
        A LangChain chat model (e.g. ``ChatAnthropic``, ``ChatOpenAI``).
    """

    supports_history = False
    """This backend does not yet seed a run from prior turns. Passing a
    session_id to ctx.agent() with it configured raises rather than silently
    starting each call from a blank conversation."""

    def __init__(self, *, llm: Any) -> None:
        self._llm = llm

    async def run(
        self,
        prompt: str,
        *,
        tools: list[Any] | None = None,
        history: list[Any] | None = None,
        agent_id: str = "",
        max_turns: int | None = None,
    ) -> AgentResult[Any]:
        """Build a ReAct agent with the given tools and invoke it."""
        from langchain_core.messages import HumanMessage

        lc_tools = _convert_tools(tools or [])

        # Build a fresh agent with exactly these tools
        from langchain.agents import create_agent

        graph = create_agent(self._llm, tools=lc_tools)

        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
        )

        return _parse_result(result)


# ---------------------------------------------------------------------------
# LOOM Tool → LangChain tool conversion (the one adapter function)
# ---------------------------------------------------------------------------


def _convert_tools(tools: list[Any]) -> list[Any]:
    """Convert a list of LOOM Tools (or LangChain-native tools) to LangChain format."""
    from workflow_builder.agents.tools import Tool as LoomTool

    converted = []
    for t in tools:
        if isinstance(t, LoomTool):
            converted.append(_loom_tool_to_langchain(t))
        else:
            # Already a LangChain tool — pass through
            converted.append(t)
    return converted


def _loom_tool_to_langchain(tool: Any) -> Any:
    """Convert a single LOOM Tool to a LangChain tool."""
    from langchain_core.tools import tool as lc_tool_decorator

    fn = tool.fn

    # Build an async wrapper that matches the original signature
    if asyncio.iscoroutinefunction(fn):
        async def wrapper(**kwargs: Any) -> Any:
            return await fn(**kwargs)
    else:
        async def wrapper(**kwargs: Any) -> Any:
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

    wrapper.__name__ = tool.name
    wrapper.__doc__ = tool.description or tool.name

    # Use LangChain's @tool decorator to create a proper Tool
    lc_tool = lc_tool_decorator(wrapper)
    lc_tool.name = tool.name
    lc_tool.description = tool.description or tool.name
    return lc_tool


# ---------------------------------------------------------------------------
# Parse LangGraph result → LOOM AgentResult
# ---------------------------------------------------------------------------


def _parse_result(result: dict[str, Any]) -> AgentResult[Any]:
    """Extract output, usage, and turn count from a LangGraph result."""
    messages = result.get("messages", [])
    output_text = ""
    turns = 0

    for msg in messages:
        if not hasattr(msg, "tool_calls"):
            continue
        turns += 1
        if hasattr(msg, "content") and msg.content:
            output_text = msg.content

    usage = Usage()
    last_ai = next(
        (m for m in reversed(messages) if hasattr(m, "tool_calls")),
        None,
    )
    if last_ai and hasattr(last_ai, "usage_metadata"):
        meta = last_ai.usage_metadata
        if isinstance(meta, dict):
            usage.input_tokens = meta.get("input_tokens", 0)
            usage.output_tokens = meta.get("output_tokens", 0)

    return AgentResult(
        output=output_text,
        agent="langchain",
        usage=usage,
        turns=turns,
    )
