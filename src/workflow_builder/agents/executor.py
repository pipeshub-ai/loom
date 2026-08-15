"""Agent executor protocol — the plug-point for any agent framework.

The workflow engine journals ``AgentResult``; it never reaches inside the
executor.  Session state, turn loops, and memory management are the
framework's concern.  This separation means LangGraph, Agno, CrewAI,
Pydantic AI, or a hand-rolled loop all look the same to the durability
layer.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from workflow_builder.agents.messages import Message
from workflow_builder.agents.result import AgentResult
from workflow_builder.agents.tools import Tool


class AgentSettings(BaseModel):
    """Per-run overrides passed to an executor."""

    max_turns: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    timeout: float | None = None
    """Timeout in seconds."""
    temperature: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentContext(BaseModel):
    """Ambient context threaded through every executor call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str = ""
    workflow: str = ""
    path: str = ""
    agent_id: str = ""
    """Stable identity of the agent being invoked, used to key its memory."""
    session_id: str | None = None
    """Conversation this call belongs to. ``None`` means a one-shot call."""
    history: list[Message] = Field(default_factory=list)
    """Prior turns to seed the conversation with, oldest first."""
    deps: Any = None

    broker: Any = None
    """Optional :class:`EffectBroker`. Set when the agent runs inside a
    workflow, so each tool call is mediated as it is made rather than only
    filtered when the toolset was resolved — an agent holds its tools for the
    length of the turn loop, and permission has to be checked where a tool is
    used rather than where it was handed out."""
    authority: Any = None
    """What the broker weighs the tool call against."""


@runtime_checkable
class AgentExecutor(Protocol):
    """Plug in any agent framework: LangGraph, Agno, Pydantic AI, or custom.

    Implementations own the turn loop — model calls, tool dispatch, memory.
    The workflow engine only sees :class:`AgentResult` at the boundary.
    """

    agent_id: str

    async def execute(
        self,
        input: Any,
        *,
        tools: list[Tool] | None = None,
        output_type: type[BaseModel] | None = None,
        settings: AgentSettings | None = None,
        context: AgentContext | None = None,
    ) -> AgentResult[Any]:
        """Run the agent to completion and return a result."""
        ...
