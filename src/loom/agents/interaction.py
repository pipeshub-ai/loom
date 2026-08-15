"""How an agent asks a human a question and gets an answer.

The authoring agent and a workflow-running agent both need the same thing:
pause, pose a question, wait for a reply. The transport is the host's
problem — a CLI reads stdin, a test injects a callback. This module owns
the protocol and two reference implementations.

``SuspendUserInteraction`` is deliberately absent: parking a durable run
mid-agent-loop is not supported (the runner journals the whole agent call
as one entry), and ``Suspend`` requires a journal ``path`` the tool does
not have. The durable primitive for a long wait is
``ctx.wait_for_approval()`` in the workflow body. In-workflow asks that
must not be re-asked on replay go through ``ctx.call`` (see
:func:`make_ask_user_tool`).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from loom.agents.tools import Tool, ToolContext, tool

__all__ = [
    "AskUserGate",
    "CLIUserInteraction",
    "CallbackUserInteraction",
    "UserInteraction",
    "UserQuestion",
    "UserResponse",
    "ask_user",
    "make_ask_user_tool",
]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_ANSWER = 2048

#: Serialises CLI prompts so two concurrent agents cannot interleave on stdin.
_prompt_lock: asyncio.Lock | None = None


def _cli_lock() -> asyncio.Lock:
    global _prompt_lock
    if _prompt_lock is None:
        _prompt_lock = asyncio.Lock()
    return _prompt_lock


class UserQuestion(BaseModel):
    """What the agent wants to know."""

    question: str
    options: list[str] | None = None
    input_type: Literal["text", "select", "confirm"] = "text"
    context: str = ""
    allow_skip: bool = False


class UserResponse(BaseModel):
    """What the user answered."""

    answer: str
    skipped: bool = False


@runtime_checkable
class UserInteraction(Protocol):
    """How the agent asks a human a question and gets an answer."""

    async def ask(self, question: UserQuestion) -> UserResponse: ...


@dataclass
class AskUserGate:
    """Per-generation budget and phase switch for :func:`make_ask_user_tool`.

    ``enabled`` is flipped off before automated repair and smoke so a model
    cannot deadlock CI by asking a question nobody is there to answer.
    """

    budget: int = 5
    asked: int = 0
    enabled: bool = True


class CLIUserInteraction:
    """Default interactive implementation: reads from stdin, writes to stderr.

    Stderr is load-bearing: under ``loom mcp --transport stdio`` stdout is the
    protocol channel and a printed question would corrupt the session. A
    non-TTY stdin (CI, a pipe) returns a skipped response rather than letting
    ``input()`` raise ``EOFError`` and kill the generation.
    """

    def __init__(self, *, timeout: float = 300.0) -> None:
        self._timeout = timeout

    async def ask(self, question: UserQuestion) -> UserResponse:
        if not sys.stdin.isatty():
            return UserResponse(answer="", skipped=True)
        async with _cli_lock():
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._ask_sync, question),
                    self._timeout,
                )
            except TimeoutError:
                return UserResponse(answer="", skipped=True)

    def _ask_sync(self, question: UserQuestion) -> UserResponse:
        if question.context:
            _stderr(f"  Context: {question.context}")

        if question.input_type == "confirm":
            return UserResponse(answer=self._confirm(question.question))
        if question.input_type == "select" and question.options:
            return UserResponse(answer=self._select(question))
        answer = self._text(question.question)
        if not answer and question.allow_skip:
            return UserResponse(answer="", skipped=True)
        return UserResponse(answer=answer)

    def _text(self, prompt: str) -> str:
        try:
            from rich.console import Console
            from rich.prompt import Prompt

            return Prompt.ask(f"  {prompt}", console=Console(stderr=True)) or ""
        except ImportError:
            _stderr(f"  Agent asks: {prompt}")
            return sys.stdin.readline().rstrip("\n")

    def _confirm(self, prompt: str) -> str:
        try:
            from rich.console import Console
            from rich.prompt import Confirm

            return "yes" if Confirm.ask(f"  {prompt}", console=Console(stderr=True)) else "no"
        except ImportError:
            _stderr(f"  Agent asks: {prompt} [y/n]")
            answer = sys.stdin.readline().strip().lower()
            return "yes" if answer in ("y", "yes") else "no"

    def _select(self, question: UserQuestion) -> str:
        options = question.options or []
        _stderr(f"  Agent asks: {question.question}")
        for index, option in enumerate(options, 1):
            _stderr(f"    {index}. {option}")
        while True:
            raw = sys.stdin.readline().strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            if raw in options:
                return raw
            _stderr(f"  Please enter 1-{len(options)} or the option text.")


class CallbackUserInteraction:
    """Wraps any callable ``(UserQuestion) -> UserResponse`` (sync or async)."""

    def __init__(
        self,
        callback: Callable[[UserQuestion], Awaitable[UserResponse] | UserResponse],
    ) -> None:
        self._callback = callback

    async def ask(self, question: UserQuestion) -> UserResponse:
        result = self._callback(question)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, UserResponse):
            result = UserResponse(answer=str(result))
        return result


@tool
async def ask_user(
    question: str,
    options: list[str] | None = None,
    input_type: str = "text",
    context: str = "",
) -> str:
    """Ask the user a clarifying question when the spec is ambiguous.

    Use ONLY when blocked — when you cannot determine from the spec alone
    what to do next. Do not ask to confirm things the spec already states.

    Args:
        question: The question to ask. Keep it short and direct.
        options: Optional predefined choices for select.
        input_type: "text", "select", or "confirm".
        context: Why you are asking (optional).
    """
    return json.dumps({"error": "ask_user is not configured for this agent"})


def make_ask_user_tool(
    interaction: UserInteraction,
    *,
    gate: AskUserGate | None = None,
) -> Tool:
    """Bind :func:`ask_user` to a :class:`UserInteraction`.

    When the agent is running inside a workflow, the answer is journaled via
    ``ctx.call`` under a name derived from the question text, so a replay
    that asks the same question is not re-prompted. Call order is not used:
    a replayed model may ask in a different sequence.
    """
    gate = gate or AskUserGate()

    async def bound(
        ctx: ToolContext,
        question: str,
        options: list[str] | None = None,
        input_type: str = "text",
        context: str = "",
    ) -> str:
        if not gate.enabled:
            return json.dumps(
                {
                    "error": (
                        "ask_user is not available in this phase — proceed "
                        "with the information you have."
                    )
                }
            )
        if gate.asked >= gate.budget:
            return json.dumps(
                {
                    "error": (
                        f"You have asked {gate.budget} questions already. "
                        "Proceed with the information you have."
                    )
                }
            )
        if input_type == "select" and not options:
            return json.dumps(
                {"error": "input_type 'select' requires a non-empty options list"}
            )
        try:
            posed = UserQuestion(
                question=question,
                options=options,
                input_type=input_type if input_type in ("text", "select", "confirm") else "text",
                context=context,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

        async def _ask() -> str:
            response = await interaction.ask(posed)
            gate.asked += 1
            if response.skipped:
                return json.dumps({"skipped": True, "answer": ""})
            return _sanitize(response.answer)

        if ctx.workflow_ctx is not None:
            digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
            return await ctx.workflow_ctx.call(f"ask_user:{digest}", _ask)
        return await _ask()

    return replace(ask_user, fn=bound, takes_context=True)


def _sanitize(answer: str) -> str:
    return _CTRL.sub("", answer)[:_MAX_ANSWER]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)
