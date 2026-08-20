"""Ask-user protocol, the three transports, and coding-agent wiring.

Shaped by three sources, each of which settled a different question: Claude
Code's ``AskUserQuestion`` for the interaction (batching, an always-present
"Other", a recommendation rendered first), MCP's ``elicitation`` for the wire
format (a deliberately flat schema, and three response actions rather than
two), and this repository's own thesis for the part neither of them does —
recording the answers, because they are inputs to a build.

See ``phases/phase-14-asking-the-user.md``.
"""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest

from loom.agents.interaction import (
    MAX_BATCH,
    Answer,
    AskedQuestion,
    AskUserGate,
    CallbackUserInteraction,
    Choice,
    CLIUserInteraction,
    Question,
    RecordedUserInteraction,
    elicitation_field,
    make_ask_user_tool,
)
from loom.agents.tools import ToolContext


def _q(question: str = "Which epic?", **kw) -> Question:
    return Question(question=question, **kw)


def _epics() -> Question:
    return Question(
        question="Which epic did you mean?",
        header="Epic",
        kind="select",
        choices=[
            Choice(value="PA-1844", label="SaaS V2"),
            Choice(value="PA-1769", label="Launch SAAS", recommended=True),
        ],
    )


class TestQuestionIdentity:
    """The recording key, and why it is derived rather than assigned."""

    def test_the_same_question_hashes_the_same(self) -> None:
        assert _epics().id == _epics().id

    def test_changing_the_choices_changes_the_question(self) -> None:
        """A stale record must not answer a question whose options moved.

        The answer was a choice *between the options that were shown*; replayed
        against a different list it is a value nobody picked.
        """
        one = _epics()
        two = one.model_copy(update={"choices": [Choice(value="PA-1", label="x")]})

        assert one.id != two.id

    def test_wording_is_part_of_it(self) -> None:
        assert _q("A?").id != _q("B?").id

    def test_the_recommendation_is_rendered_first(self) -> None:
        assert [c.value for c in _epics().ordered()] == ["PA-1769", "PA-1844"]


class TestCallbackUserInteraction:
    async def test_a_per_question_callable_is_accepted(self) -> None:
        seen: list[str] = []

        def cb(question: Question) -> Answer:
            seen.append(question.question)
            return Answer(action="accept", values=["PA-1769"])

        answers = await CallbackUserInteraction(cb).ask([_epics()])

        assert seen == ["Which epic did you mean?"]
        assert answers[0].values == ["PA-1769"]

    async def test_a_bare_string_is_wrapped(self) -> None:
        answers = await CallbackUserInteraction(lambda q: "plain").ask([_q()])

        assert answers[0].action == "accept"
        assert answers[0].text == "plain"

    async def test_one_answer_per_question(self) -> None:
        answers = await CallbackUserInteraction(lambda q: "x").ask([_q("a"), _q("b")])

        assert len(answers) == 2


class TestCLIUserInteraction:
    def _offline(self, monkeypatch: pytest.MonkeyPatch, typed: list[str]) -> StringIO:
        """A CLI with no rich, a scripted stdin, and captured streams."""
        stdout, stderr = StringIO(), StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        lines = iter([f"{line}\n" for line in typed])
        monkeypatch.setattr(sys.stdin, "readline", lambda: next(lines, ""))

        class _Boom:
            def __getattr__(self, name: str) -> None:
                raise ImportError

        for module in ("rich", "rich.console", "rich.prompt"):
            monkeypatch.setitem(sys.modules, module, _Boom())
        self._stdout = stdout
        return stderr

    async def test_a_non_tty_is_unavailable_rather_than_slow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capability, not timeout. The tool is never offered, so nothing waits
        five minutes to discover there was nobody there."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        assert CLIUserInteraction().available() is False
        assert (await CLIUserInteraction().ask([_q()]))[0].action == "cancel"

    def test_prompts_go_to_stderr_not_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under ``loom mcp --transport stdio`` stdout is the protocol."""
        stderr = self._offline(monkeypatch, ["Ada"])

        answers = CLIUserInteraction()._ask_sync([_q("Name?")])

        assert answers[0].text == "Ada"
        assert self._stdout.getvalue() == ""
        assert "Name?" in stderr.getvalue()

    def test_a_number_picks_a_choice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stderr = self._offline(monkeypatch, ["2"])

        answers = CLIUserInteraction()._ask_sync([_epics()])

        # Rendered recommendation-first, so 2 is the *other* one.
        assert answers[0].values == ["PA-1844"]
        assert "(recommended)" in stderr.getvalue()

    def test_free_text_is_accepted_off_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"Other" reachable by typing, not only by finding its number.

        A closed list is a claim about the answer space, and the agent built it
        from a lookup that may have missed.
        """
        self._offline(monkeypatch, ["neither, use PA-2000"])

        answers = CLIUserInteraction()._ask_sync([_epics()])

        assert answers[0].other == "neither, use PA-2000"

    def test_an_off_list_answer_is_refused_when_the_list_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._offline(monkeypatch, ["nonsense", "1"])
        closed = _epics().model_copy(update={"allow_other": False})

        answers = CLIUserInteraction()._ask_sync([closed])

        assert answers[0].values == ["PA-1769"]

    def test_multi_select_takes_several(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._offline(monkeypatch, ["1,2"])
        both = _epics().model_copy(update={"kind": "multi_select"})

        answers = CLIUserInteraction()._ask_sync([both])

        assert sorted(answers[0].values) == ["PA-1769", "PA-1844"]

    def test_empty_declines_to_the_recommendation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing Enter is a usable answer, which is the point of marking a
        recommendation at all."""
        self._offline(monkeypatch, [""])

        answers = CLIUserInteraction()._ask_sync([_epics()])

        assert answers[0].action == "decline"
        assert answers[0].values == ["PA-1769"]

    def test_a_closed_stdin_cancels_the_whole_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partial list would misalign answers with questions silently."""
        self._offline(monkeypatch, [])

        answers = CLIUserInteraction()._ask_sync([_q("a"), _q("b"), _q("c")])

        assert [a.action for a in answers] == ["cancel", "cancel", "cancel"]

    def test_a_batch_is_numbered_so_it_can_be_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stderr = self._offline(monkeypatch, ["x", "y"])

        CLIUserInteraction()._ask_sync([_q("a"), _q("b")])

        assert "[1/2]" in stderr.getvalue() and "[2/2]" in stderr.getvalue()


class TestThreeActionsAreDistinct:
    """MCP's three-state enum, kept because the states want different code.

    ``decline`` is "you decide" and ``cancel`` is "nobody was there". They lead
    to opposite fallbacks — take the recommendation, versus defer the decision
    to run time — and collapsing them is what made a five-minute silence look
    like a considered answer.
    """

    def test_decline_carries_the_recommendation(self) -> None:
        assert Answer(action="decline", values=["PA-1769"]).answered is False

    def test_cancel_carries_nothing(self) -> None:
        assert Answer(action="cancel").text == ""

    def test_only_accept_counts_as_answered(self) -> None:
        assert Answer(action="accept", other="x").answered is True
        assert Answer(action="decline").answered is False
        assert Answer(action="cancel").answered is False


class TestRecordedUserInteraction:
    """What makes a generation reproducible, and ``loom author`` CI-able."""

    def _record(self) -> list[AskedQuestion]:
        return [
            AskedQuestion(
                question=_epics(), answer=Answer(action="accept", values=["PA-1769"])
            )
        ]

    async def test_a_recorded_answer_is_replayed(self) -> None:
        answers = await RecordedUserInteraction(self._record()).ask([_epics()])

        assert answers[0].values == ["PA-1769"]

    async def test_an_unrecorded_question_falls_through(self) -> None:
        """A spec that grew a new ambiguity should ask about it, not refuse to
        build."""
        played = RecordedUserInteraction(
            self._record(), fallback=CallbackUserInteraction(lambda q: "fresh")
        )

        answers = await played.ask([_epics(), _q("something new?")])

        assert answers[0].values == ["PA-1769"]
        assert answers[1].other == "fresh"

    async def test_without_a_fallback_an_unknown_question_cancels(self) -> None:
        answers = await RecordedUserInteraction(self._record()).ask([_q("new?")])

        assert answers[0].action == "cancel"

    async def test_a_changed_question_is_not_answered_from_the_record(self) -> None:
        moved = _epics().model_copy(
            update={"choices": [Choice(value="PA-9", label="Something else")]}
        )

        answers = await RecordedUserInteraction(self._record()).ask([moved])

        assert answers[0].action == "cancel"

    def test_it_round_trips_through_a_file(self, tmp_path) -> None:
        path = tmp_path / "answers.json"
        path.write_text(
            json.dumps([row.model_dump(mode="json") for row in self._record()])
        )

        played = RecordedUserInteraction.from_file(path)

        assert played.available() is True


class TestElicitationSchema:
    """Every question kind must render inside MCP's restricted subset.

    "Complex nested structures … are intentionally not supported to simplify
    client user experience" — so a client is entitled to reject anything richer,
    and a question shape that cannot be expressed here is one the MCP transport
    silently cannot ask.
    """

    @staticmethod
    def _schema(question: Question) -> dict:
        from pydantic import create_model

        model = create_model("A", answer=elicitation_field(question))
        return model.model_json_schema()

    def test_a_select_becomes_an_enum(self) -> None:
        schema = json.dumps(self._schema(_epics()))

        assert "PA-1769" in schema and "PA-1844" in schema

    def test_a_confirm_becomes_a_boolean(self) -> None:
        schema = self._schema(_q(kind="confirm"))

        assert "boolean" in json.dumps(schema)

    def test_a_multi_select_becomes_an_array(self) -> None:
        schema = self._schema(_epics().model_copy(update={"kind": "multi_select"}))

        assert schema["properties"]["answer"]["type"] == "array"

    def test_a_text_question_is_a_plain_string(self) -> None:
        assert self._schema(_q())["properties"]["answer"]["type"] == "string"

    def test_nothing_nests(self) -> None:
        """The subset is flat. A nested object would be valid JSON Schema and
        an invalid elicitation."""
        for question in (_q(), _epics(), _q(kind="confirm")):
            properties = self._schema(question)["properties"]
            for spec in properties.values():
                assert spec.get("type") != "object"


class TestAskUserTool:
    async def test_a_batch_costs_one_call(self) -> None:
        calls = 0

        def cb(questions: list[Question]) -> list[Answer]:
            nonlocal calls
            calls += 1
            return [Answer(action="accept", other="x") for _ in questions]

        tool = make_ask_user_tool(CallbackUserInteraction(cb))
        payload = json.loads(
            await tool.invoke(
                {"questions": [{"question": "a?"}, {"question": "b?"}]}, ToolContext()
            )
        )

        assert calls == 1
        assert len(payload["answers"]) == 2

    async def test_too_many_questions_is_refused_not_truncated(self) -> None:
        tool = make_ask_user_tool(CallbackUserInteraction(lambda q: "x"))

        payload = json.loads(
            await tool.invoke(
                {"questions": [{"question": f"q{n}?"} for n in range(MAX_BATCH + 1)]},
                ToolContext(),
            )
        )

        assert "error" in payload

    async def test_the_budget_counts_questions_not_calls(self) -> None:
        """Otherwise batching buys extra questions, which is the opposite of
        what it is for."""
        tool = make_ask_user_tool(
            CallbackUserInteraction(lambda q: "x"), gate=AskUserGate(budget=2)
        )
        ctx = ToolContext()

        first = json.loads(
            await tool.invoke(
                {"questions": [{"question": "a?"}, {"question": "b?"}]}, ctx
            )
        )
        second = json.loads(
            await tool.invoke({"questions": [{"question": "c?"}]}, ctx)
        )

        assert len(first["answers"]) == 2
        assert "error" in second

    async def test_a_disabled_gate_refuses_without_asking(self) -> None:
        asked: list = []
        tool = make_ask_user_tool(
            CallbackUserInteraction(lambda q: asked.append(q) or "x"),
            gate=AskUserGate(enabled=False),
        )

        payload = json.loads(
            await tool.invoke({"questions": [{"question": "blocked?"}]}, ToolContext())
        )

        assert asked == []
        assert "error" in payload

    async def test_answers_are_capped_and_control_characters_stripped(self) -> None:
        tool = make_ask_user_tool(
            CallbackUserInteraction(lambda q: "ok\x00" + ("z" * 5000))
        )

        payload = json.loads(
            await tool.invoke({"questions": [{"question": "x?"}]}, ToolContext())
        )

        assert "\x00" not in payload["answers"][0]["other"]
        assert len(payload["answers"][0]["other"]) == 2048

    async def test_bare_string_choices_are_accepted(self) -> None:
        """A model writes ``choices: ["a", "b"]`` about as often as the object
        form; refusing it costs a turn to say so."""
        seen: list[Question] = []
        tool = make_ask_user_tool(
            CallbackUserInteraction(lambda q: seen.append(q) or "a")
        )

        await tool.invoke(
            {"questions": [{"question": "which?", "choices": ["a", "b"]}]},
            ToolContext(),
        )

        assert [c.value for c in seen[0].choices] == ["a", "b"]
        assert seen[0].kind == "select"

    async def test_it_records_what_was_asked(self) -> None:
        record: list[AskedQuestion] = []
        tool = make_ask_user_tool(
            CallbackUserInteraction(lambda q: "Acme"), record=record
        )

        await tool.invoke({"questions": [{"question": "Which company?"}]}, ToolContext())

        assert len(record) == 1
        assert record[0].question.question == "Which company?"
        assert record[0].answer.other == "Acme"
