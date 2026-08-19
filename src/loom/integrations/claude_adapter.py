"""Anthropic Claude Messages API adapter.

Direction A: ``ClaudeExecutor`` wraps the Anthropic Messages API
tool-call loop and exposes it via the ``AgentExecutor`` protocol.

Direction B: ``workflow_as_claude_tool`` returns a tool schema dict
that lets a Claude conversation invoke a LOOM workflow.
"""

from __future__ import annotations

from typing import Any

from loom.integrations.base import AgentExecutor
from loom.integrations.structured import coerce_output


def _build_tool_schema(
    workflow_id: str, description: str
) -> dict[str, Any]:
    """Return a Claude-compatible tool-schema dict."""
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


class ClaudeExecutor:
    """Wraps the Anthropic Messages API as an ``AgentExecutor``.

    Runs a tool-call loop: send messages, process tool_use
    blocks, feed results back, repeat until the model stops
    calling tools or *max_turns* is reached.

    Parameters
    ----------
    model:
        Anthropic model identifier (e.g. ``"claude-sonnet-4-20250514"``).
    max_turns:
        Maximum number of tool-call round-trips.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        max_turns: int = 10,
    ) -> None:
        self._model = model
        self._max_turns = max_turns

    async def execute(
        self,
        *,
        input: str,
        tools: list[Any] | None = None,
        output_type: type | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Run a Claude tool-call loop on *input*.

        Uses the ``anthropic.AsyncAnthropic`` client to send
        messages and iteratively resolve tool calls.
        """
        import anthropic

        client = anthropic.AsyncAnthropic()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": input},
        ]
        api_tools = tools or []

        extra: dict[str, Any] = {}
        if settings:
            if "max_tokens" in settings:
                extra["max_tokens"] = settings["max_tokens"]
            if "temperature" in settings:
                extra["temperature"] = settings[
                    "temperature"
                ]

        max_tokens = extra.pop("max_tokens", 1024)

        for _ in range(self._max_turns):
            response = await client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=messages,
                tools=api_tools or [],
                **extra,
            )

            # Check if the model wants to call a tool.
            tool_use_blocks = [
                b
                for b in response.content
                if getattr(b, "type", None) == "tool_use"
            ]
            if not tool_use_blocks:
                # No tool calls -- extract text and return.
                text_parts = [
                    b.text
                    for b in response.content
                    if getattr(b, "type", None) == "text"
                ]
                return "\n".join(text_parts)

            # Append assistant message, then stub tool
            # results so the loop can continue.
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                }
            )
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": "Tool execution not wired.",
                }
                for b in tool_use_blocks
            ]
            messages.append(
                {"role": "user", "content": tool_results}
            )

        # Exhausted turns -- return last response text.
        return coerce_output(str(response.content), output_type)


# -- static protocol check ---------------------------------------------------
def _check() -> None:
    _: AgentExecutor = ClaudeExecutor()


# -- Direction B --------------------------------------------------------------


def workflow_as_claude_tool(
    runtime: Any,
    workflow_id: str,
    description: str,
) -> dict[str, Any]:
    """Create a Claude-compatible tool schema for a workflow.

    Parameters
    ----------
    runtime:
        A ``loom.Runtime`` instance.
    workflow_id:
        The registered workflow name.
    description:
        Human-readable description shown to the model.

    Returns
    -------
    dict
        A tool-schema dict with ``name``, ``description``, and
        ``input_schema`` keys suitable for the Anthropic Messages
        API ``tools`` parameter.
    """
    return _build_tool_schema(workflow_id, description)
