"""Ask-user protocol, CLI hardening, and coding-agent wiring."""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest

from loom.agents.interaction import (
    AskUserGate,
    CallbackUserInteraction,
    CLIUserInteraction,
    UserQuestion,
    UserResponse,
    make_ask_user_tool,
)
from loom.agents.tools import ToolContext


class TestCallbackUserInteraction:
    async def test_sync_callback(self) -> None:
        seen: list[str] = []

        def cb(question: UserQuestion) -> UserResponse:
            seen.append(question.question)
            return UserResponse(answer="blue")

        response = await CallbackUserInteraction(cb).ask(
            UserQuestion(question="colour?")
        )
        assert seen == ["colour?"]
        assert response.answer == "blue"

    async def test_async_callback(self) -> None:
        async def cb(question: UserQuestion) -> UserResponse:
            return UserResponse(answer="yes")

        response = await CallbackUserInteraction(cb).ask(
            UserQuestion(question="ok?", input_type="confirm")
        )
        assert response.answer == "yes"

    async def test_bare_string_is_wrapped(self) -> None:
        response = await CallbackUserInteraction(lambda q: "plain").ask(
            UserQuestion(question="x")
        )
        assert response.answer == "plain"


class TestCLIUserInteraction:
    async def test_non_tty_returns_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        response = await CLIUserInteraction().ask(UserQuestion(question="anything?"))
        assert response.skipped is True
        assert response.answer == ""

    def test_prompts_go_to_stderr_not_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stdout, stderr = StringIO(), StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr(sys.stdin, "readline", lambda: "Ada\n")

        class _Boom:
            def __getattr__(self, name: str) -> None:
                raise ImportError

        monkeypatch.setitem(sys.modules, "rich", _Boom())
        monkeypatch.setitem(sys.modules, "rich.console", _Boom())
        monkeypatch.setitem(sys.modules, "rich.prompt", _Boom())

        response = CLIUserInteraction()._ask_sync(UserQuestion(question="Name?"))
        assert response.answer == "Ada"
        assert stdout.getvalue() == ""
        assert "Name?" in stderr.getvalue()


class TestAskUserTool:
    async def test_budget_caps_questions(self) -> None:
        calls = 0

        def cb(question: UserQuestion) -> UserResponse:
            nonlocal calls
            calls += 1
            return UserResponse(answer="x")

        tool = make_ask_user_tool(
            CallbackUserInteraction(cb), gate=AskUserGate(budget=1)
        )
        ctx = ToolContext()
        first = await tool.invoke({"question": "one?"}, ctx)
        second = json.loads(await tool.invoke({"question": "two?"}, ctx))
        assert first == "x"
        assert calls == 1
        assert "error" in second
        assert "1 questions" in second["error"] or "asked" in second["error"]

    async def test_disabled_gate_refuses_without_asking(self) -> None:
        asked = []
        gate = AskUserGate(enabled=False)
        tool = make_ask_user_tool(
            CallbackUserInteraction(lambda q: asked.append(q) or UserResponse(answer="x")),
            gate=gate,
        )
        payload = json.loads(
            await tool.invoke({"question": "blocked?"}, ToolContext())
        )
        assert asked == []
        assert "error" in payload

    async def test_answers_are_capped_and_control_characters_stripped(self) -> None:
        tool = make_ask_user_tool(
            CallbackUserInteraction(
                lambda q: UserResponse(answer="ok\x00" + ("z" * 5000))
            )
        )
        answer = await tool.invoke({"question": "x?"}, ToolContext())
        assert "\x00" not in answer
        assert len(answer) == 2048


class TestCodingAgentAskUser:
    async def test_asks_receives_and_uses_the_answer(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent
        from loom.agents.messages import ToolCall
        from loom.agents.output import FINAL_OUTPUT_TOOL
        from loom.testing.mock import MockModelProvider, mock_response

        code = (
            "from loom import Context, step, workflow\n"
            "\n"
            "@step\n"
            "async def greet(name: str) -> str:\n"
            "    return f'hello {name}'\n"
            "\n"
            "@workflow(name='greet')\n"
            "async def greet_flow(ctx: Context, name: str) -> str:\n"
            "    return await ctx.step(greet, name)\n"
        )
        asked: list[str] = []

        def cb(question: UserQuestion) -> UserResponse:
            asked.append(question.question)
            return UserResponse(answer="Acme")

        provider = MockModelProvider(
            responses=[
                mock_response(
                    tool_calls=[
                        ToolCall(
                            name="ask_user",
                            arguments={"question": "Which company?"},
                        )
                    ]
                ),
                mock_response(
                    tool_calls=[
                        ToolCall(
                            name=FINAL_OUTPUT_TOOL,
                            arguments={
                                "code": code,
                                "explanation": "Greets Acme",
                                "plan": [],
                            },
                        )
                    ]
                ),
            ]
        )
        agent = WorkflowCodingAgent(
            provider,
            user_interaction=CallbackUserInteraction(cb),
            smoke_test=False,
            max_repair_attempts=0,
        )
        result = await agent.generate("greet a user")
        assert asked == ["Which company?"]
        assert "greet" in result.code
        assert "Asking the user" in agent.build_system_prompt()

    def test_prompt_omits_ask_section_without_interaction(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent(model=object(), smoke_test=False)
        assert "ask_user" not in agent.build_system_prompt()
