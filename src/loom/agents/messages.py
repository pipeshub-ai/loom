"""Conversation primitives.

Provider-neutral and journal-safe: these are Pydantic models so an entire conversation can
be written to the journal and rehydrated on a different machine after a restart.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from loom.core.ids import new_id


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """A model's request to invoke a tool."""

    id: str = Field(default_factory=lambda: new_id("call"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One turn in a conversation."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def text(self) -> str:
        return self.content or ""


def system(content: str) -> Message:
    return Message(role=Role.SYSTEM, content=content)


def user(content: str) -> Message:
    return Message(role=Role.USER, content=content)


def assistant(content: str | None = None, tool_calls: list[ToolCall] | None = None) -> Message:
    return Message(role=Role.ASSISTANT, content=content, tool_calls=tool_calls or [])


def tool_result(call_id: str, content: str, *, name: str | None = None) -> Message:
    return Message(role=Role.TOOL, content=content, tool_call_id=call_id, name=name)


def retry_prompt(content: str, *, call_id: str | None = None) -> Message:
    """Corrective feedback sent back to the model after a validation failure.

    Feeding the actual error text back is what makes self-correction work; a bare "try
    again" gives the model nothing to act on.
    """
    if call_id:
        return Message(
            role=Role.TOOL,
            content=content,
            tool_call_id=call_id,
            metadata={"retry_prompt": True},
        )
    return Message(role=Role.USER, content=content, metadata={"retry_prompt": True})
