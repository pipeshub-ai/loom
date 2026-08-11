"""What an agent run produces."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from workflow_builder.agents.messages import Message, ToolCall
from workflow_builder.core.models import Usage

OutputT = TypeVar("OutputT")


class ItemKind(StrEnum):
    """Semantic events emitted during a run, for streaming and for audit."""

    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    HANDOFF = "handoff"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    GUARDRAIL = "guardrail"
    RETRY = "retry"
    FINAL_OUTPUT = "final_output"


class RunItem(BaseModel):
    """One thing that happened, in order."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: ItemKind
    agent: str = ""
    name: str = ""
    content: Any = None
    turn: int = 0


class ApprovalRequest(BaseModel):
    """A tool call paused pending a human decision."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent: str = ""

    @property
    def subject(self) -> str:
        """Stable key used to deliver the decision back to the run."""
        return f"{self.tool_name}:{self.tool_call_id}"


class AgentResult(BaseModel, Generic[OutputT]):
    """The outcome of an agent run, plus everything needed to audit it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    output: Any = None
    agent: str = ""
    messages: list[Message] = Field(default_factory=list)
    """The full conversation, ready to seed the next turn."""
    items: list[RunItem] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    turns: int = 0
    tool_calls: list[ToolCall] = Field(default_factory=list)
    interruptions: list[ApprovalRequest] = Field(default_factory=list)
    """Non-empty when the run stopped to await human approval outside a workflow."""
    handoffs: list[str] = Field(default_factory=list)

    @property
    def interrupted(self) -> bool:
        return bool(self.interruptions)

    def last_message(self) -> Message | None:
        return self.messages[-1] if self.messages else None

    def text(self) -> str:
        if isinstance(self.output, str):
            return self.output
        message = self.last_message()
        return message.text() if message else ""

    def tools_used(self) -> list[str]:
        return [call.name for call in self.tool_calls]
