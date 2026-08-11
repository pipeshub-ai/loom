"""LangChain/LangGraph → LOOM Agent bridge.

This adapter implements the LOOM ``AgentExecutor`` protocol
(``agents/executor.py``) so a LangGraph compiled agent can be plugged
into a LOOM ``Agent`` via ``Agent(executor=LangChainAgentExecutor(graph))``.

The workflow code only sees ``ctx.agent(agent, input)`` and
``AgentResult`` — it never knows LangChain is running underneath.

Example::

    from langchain.agents import create_agent
    from langchain_anthropic import ChatAnthropic
    from workflow_builder.agents.agent import Agent
    from workflow_builder.integrations.langgraph_agent import (
        LangChainAgentExecutor,
    )

    llm = ChatAnthropic(model="claude-sonnet-4-6")
    graph = create_agent(llm, tools=[search_tool, fetch_tool])

    researcher = Agent(
        name="researcher",
        executor=LangChainAgentExecutor(graph),
    )

    # Inside a workflow:
    result = await ctx.agent(researcher, "Find latest AI news")
    print(result.output)  # text from the LangChain agent
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from workflow_builder.agents.executor import AgentContext, AgentSettings
from workflow_builder.agents.result import AgentResult
from workflow_builder.agents.tools import Tool
from workflow_builder.core.models import Usage


class LangChainAgentExecutor:
    """Bridge a LangGraph compiled agent into the LOOM Agent system.

    Parameters
    ----------
    graph:
        A compiled LangGraph agent (from ``create_agent()`` or
        ``StateGraph(...).compile()``).
    agent_id:
        Name for logging / journaling.
    """

    def __init__(
        self,
        graph: Any,
        *,
        agent_id: str = "langchain",
    ) -> None:
        self.agent_id = agent_id
        self._graph = graph

    async def execute(
        self,
        input: Any,
        *,
        tools: list[Tool] | None = None,
        output_type: type[BaseModel] | None = None,
        settings: AgentSettings | None = None,
        context: AgentContext | None = None,
    ) -> AgentResult[Any]:
        """Invoke the LangGraph agent and return a LOOM AgentResult."""
        from langchain_core.messages import HumanMessage

        invoke_input: dict[str, Any] = {
            "messages": [HumanMessage(content=str(input))],
        }

        config: dict[str, Any] = {}
        if settings and settings.extra:
            config["configurable"] = settings.extra

        result = await self._graph.ainvoke(
            invoke_input, config=config or None
        )

        messages = result.get("messages", [])
        output_text = ""
        turns = 0

        for msg in messages:
            # Identify AI messages: they have a tool_calls attribute
            # (present on AIMessage, absent on HumanMessage/ToolMessage)
            if not hasattr(msg, "tool_calls"):
                continue
            turns += 1
            if hasattr(msg, "content") and msg.content:
                output_text = msg.content

        # Extract token usage from the last AI message if available
        usage = Usage()
        if messages:
            last_ai = next(
                (
                    m for m in reversed(messages)
                    if hasattr(m, "tool_calls")
                ),
                None,
            )
            if last_ai and hasattr(last_ai, "usage_metadata"):
                meta = last_ai.usage_metadata or {}
                if isinstance(meta, dict):
                    usage.input_tokens = meta.get(
                        "input_tokens", 0
                    )
                    usage.output_tokens = meta.get(
                        "output_tokens", 0
                    )

        return AgentResult(
            output=output_text,
            agent=self.agent_id,
            usage=usage,
            turns=turns,
        )
