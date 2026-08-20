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
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from loom.agents.tools import Tool, ToolContext, tool

__all__ = [
    "MAX_BATCH",
    "MAX_CHOICES",
    "Answer",
    "AskUserGate",
    "AskedQuestion",
    "CLIUserInteraction",
    "CallbackUserInteraction",
    "Choice",
    "ElicitationUserInteraction",
    "Question",
    "RecordedUserInteraction",
    "UserInteraction",
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


#: A question carries at most this many choices, and a batch at most this many
#: questions. Both follow Claude Code's ``AskUserQuestion``, which is the only
#: widely-used version of this tool whose limits are legible.
MAX_CHOICES = 8
MAX_BATCH = 4


class Choice(BaseModel):
    """One answer a question offers.

    ``value`` and ``label`` are separate because the agent wants an identifier
    and the person wants a sentence: ``value="PA-1769"`` reads to a human as
    ``label="Launch SAAS"``. Collapsing them puts the identifier on screen and
    the sentence in the code, which is backwards in both places.
    """

    model_config = ConfigDict(frozen=True)

    value: str
    label: str = ""
    description: str = ""
    recommended: bool = False
    """Rendered first and marked, and used as the answer when the person
    declines. A question with no recommendation is one the agent has not
    thought about — it costs the reader a decision the asker could have made."""

    def shown(self) -> str:
        return self.label or self.value


class Question(BaseModel):
    """One thing the agent needs decided.

    Shaped as the *intersection* of two consumers rather than for either: a
    terminal prompt, and MCP's ``requestedSchema``, whose subset is
    deliberately flat — "complex nested structures … are intentionally not
    supported to simplify client user experience". Anything expressible here is
    expressible in both, so neither transport needs a model of its own.
    """

    model_config = ConfigDict(frozen=True)

    question: str
    header: str = ""
    """<= 12 characters, rendered as a chip. Questions are scanned before they
    are read, and a batch of four with no labels is a wall."""
    kind: Literal["text", "select", "multi_select", "confirm"] = "text"
    choices: list[Choice] = Field(default_factory=list)
    default: str | None = None
    allow_other: bool = True
    """Whether an off-list answer is accepted. **On by default**, following
    Claude Code, where "Other" is always present: a closed list is a claim
    about the answer space, and the agent's list came from a lookup that may
    have missed. ``False`` only where the options genuinely exhaust it."""
    context: str = ""

    @property
    def id(self) -> str:
        """A content hash, and the key a recorded answer is stored under.

        Derived rather than assigned, and from the choices as well as the text,
        so a question whose options changed is a *different* question and is
        asked again rather than answered from a stale record. Position is not
        an identity here for the reason ``make_ask_user_tool`` already avoids
        it: a replayed model may ask in a different order.
        """
        material = json.dumps(
            {
                "question": self.question,
                "kind": self.kind,
                "choices": [c.value for c in self.choices],
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def recommended(self) -> Choice | None:
        for choice in self.choices:
            if choice.recommended:
                return choice
        return None

    def ordered(self) -> list[Choice]:
        """Choices with the recommendation first, as Claude Code renders it."""
        return sorted(self.choices, key=lambda c: not c.recommended)


class Answer(BaseModel):
    """What came back.

    ``action`` is MCP's three-state enum **exactly**, rather than a local one
    mapped at the boundary, because two 3-state enums with the same meaning are
    a place for them to drift.
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["accept", "decline", "cancel"] = "accept"
    values: list[str] = Field(default_factory=list)
    other: str = ""
    """Free text, when the person answered off-list."""

    @property
    def text(self) -> str:
        return self.other or ", ".join(self.values)

    @property
    def answered(self) -> bool:
        return self.action == "accept" and bool(self.values or self.other)


class AskedQuestion(BaseModel):
    """A question and its answer, recorded on the generation that asked it.

    The pair rather than the answer alone: an answer keyed by a hash is
    unreadable, and the record has to be reviewable by whoever inherits the
    workflow.
    """

    question: Question
    answer: Answer


@runtime_checkable
class UserInteraction(Protocol):
    """How the agent asks a human and gets answers.

    **Batch-native.** One call carries up to :data:`MAX_BATCH` questions,
    because the alternative costs a model turn and an interruption per
    ambiguity — four questions, four round trips, four times the person is
    pulled back to the terminal.
    """

    async def ask(self, questions: list[Question]) -> list[Answer]: ...

    def available(self) -> bool:
        """Whether anything can actually answer right now.

        A **capability** rather than a timeout, and the difference is what a
        failure looks like: a tool that is offered and cannot be answered
        blocks until it gives up, and a tool that is never offered costs
        nothing. Absence omits ``ask_user`` from the tool list entirely — the
        rule ``observe_target`` follows.
        """
        return True


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
    """Reads stdin, writes stderr.

    Stderr is load-bearing: under ``loom mcp --transport stdio`` stdout is the
    protocol channel and a printed question would corrupt the session. (That
    server should use :class:`ElicitationUserInteraction` rather than this, but
    a stray print from the wrong one must not be fatal.)

    A non-TTY reports itself unavailable through :meth:`available`, so the tool
    is not offered at all — the question never reaches a pipe that cannot
    answer it. The check remains here as well, because availability is sampled
    once when tools are built and stdin can be redirected after.
    """

    def __init__(self, *, timeout: float = 300.0) -> None:
        self._timeout = timeout

    def available(self) -> bool:
        return sys.stdin.isatty()

    async def ask(self, questions: list[Question]) -> list[Answer]:
        if not self.available():
            return [Answer(action="cancel") for _ in questions]
        async with _cli_lock():
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._ask_sync, questions),
                    self._timeout,
                )
            except TimeoutError:
                # Cancel, never decline: nobody chose anything. The two lead to
                # different fallbacks and conflating them is what made a
                # five-minute silence look like a considered "proceed".
                return [Answer(action="cancel") for _ in questions]

    def _ask_sync(self, questions: list[Question]) -> list[Answer]:
        answers: list[Answer] = []
        for index, question in enumerate(questions, 1):
            counter = f"[{index}/{len(questions)}] " if len(questions) > 1 else ""
            chip = f"({question.header}) " if question.header else ""
            _stderr(f"\n  {counter}{chip}{question.question}")
            if question.context:
                _stderr(f"    {question.context}")
            try:
                answers.append(self._one(question))
            except (EOFError, KeyboardInterrupt):
                # Ctrl-C or a closed stdin ends the whole batch, and everything
                # after it is cancelled rather than left unasked: a partial
                # list would silently misalign answers with questions.
                answers.append(Answer(action="cancel"))
                answers += [Answer(action="cancel")] * (len(questions) - index)
                break
        return answers

    def _one(self, question: Question) -> Answer:
        if question.kind == "confirm":
            return self._confirm(question)
        if question.choices:
            return self._choose(question)
        return self._text(question)

    def _text(self, question: Question) -> Answer:
        raw = self._read("    > ").strip()
        if not raw:
            return self._declined(question)
        return Answer(action="accept", other=raw)

    def _confirm(self, question: Question) -> Answer:
        suffix = "[y/n]" if question.default is None else f"[y/n, default {question.default}]"
        raw = self._read(f"    {suffix} > ").strip().lower()
        if not raw:
            if question.default is None:
                return self._declined(question)
            raw = question.default.lower()
        return Answer(action="accept", values=["yes" if raw in ("y", "yes", "true") else "no"])

    def _choose(self, question: Question) -> Answer:
        shown = question.ordered()
        for number, choice in enumerate(shown, 1):
            mark = " (recommended)" if choice.recommended else ""
            _stderr(f"    {number}. {choice.shown()}{mark}")
            if choice.description:
                _stderr(f"       {choice.description}")
        if question.allow_other:
            _stderr(f"    {len(shown) + 1}. Other — type your own answer")

        multiple = question.kind == "multi_select"
        hint = "numbers separated by commas" if multiple else "a number"
        while True:
            raw = self._read(f"    Choose {hint} > ").strip()
            if not raw:
                return self._declined(question)
            picked, other = self._parse(raw, shown, question, multiple)
            if picked is not None or other:
                return Answer(action="accept", values=picked or [], other=other)
            _stderr(f"    Enter 1-{len(shown) + (1 if question.allow_other else 0)}.")

    def _parse(
        self, raw: str, shown: list[Choice], question: Question, multiple: bool
    ) -> tuple[list[str] | None, str]:
        wanted = [part.strip() for part in raw.split(",")] if multiple else [raw]
        values: list[str] = []
        for part in wanted:
            if part.isdigit():
                number = int(part)
                if 1 <= number <= len(shown):
                    values.append(shown[number - 1].value)
                    continue
                if question.allow_other and number == len(shown) + 1:
                    typed = self._read("    Your answer > ").strip()
                    return (None, typed) if typed else (None, "")
                return None, ""
            match = [c for c in shown if part in (c.value, c.shown())]
            if not match:
                # Free text where a number was expected is an off-list answer,
                # which is what `allow_other` exists to accept. Rejecting it
                # would make the escape hatch reachable only by finding its
                # number, which is not how anyone types.
                return (None, raw) if question.allow_other else (None, "")
            values.append(match[0].value)
        return values, ""

    def _declined(self, question: Question) -> Answer:
        """An empty answer is a decline — "you decide" — not a cancel.

        So it takes the recommendation when there is one, which is what makes
        pressing Enter a usable answer rather than an abandoned question.
        """
        recommended = question.recommended()
        if recommended is not None:
            return Answer(action="decline", values=[recommended.value])
        return Answer(action="decline")

    def _read(self, prompt: str) -> str:
        try:
            from rich.console import Console
            from rich.prompt import Prompt

            return Prompt.ask(prompt.rstrip("> ").rstrip(), console=Console(stderr=True)) or ""
        except ImportError:
            _stderr(prompt)
            line = sys.stdin.readline()
            if not line:
                raise EOFError from None
            return line.rstrip("\n")


class RecordedUserInteraction:
    """Replays answers recorded on an earlier generation.

    What makes ``loom author`` runnable in CI at all once the agent starts
    asking, and what makes a generation reproducible: the same spec and the
    same answers produce the same file. Without it the answers are an
    unrecorded input to a build, which is the one thing this repository
    consistently refuses elsewhere.

    Keyed by :attr:`Question.id`, a content hash — so a question whose wording
    or choices changed is not answered from a stale record, it is passed to
    *fallback*. Falling through rather than failing is deliberate: a spec that
    grew a new ambiguity should ask about it, not refuse to build.
    """

    def __init__(
        self,
        recorded: list[AskedQuestion] | dict[str, Answer],
        *,
        fallback: UserInteraction | None = None,
    ) -> None:
        if isinstance(recorded, dict):
            self._answers = dict(recorded)
        else:
            self._answers = {item.question.id: item.answer for item in recorded}
        self._fallback = fallback

    def available(self) -> bool:
        return bool(self._answers) or bool(
            self._fallback is not None and self._fallback.available()
        )

    async def ask(self, questions: list[Question]) -> list[Answer]:
        missing = [q for q in questions if q.id not in self._answers]
        fresh: dict[str, Answer] = {}
        if missing and self._fallback is not None and self._fallback.available():
            fresh = dict(
                zip(
                    [q.id for q in missing],
                    await self._fallback.ask(missing),
                    strict=False,
                )
            )
        return [
            self._answers.get(q.id) or fresh.get(q.id) or Answer(action="cancel")
            for q in questions
        ]

    @classmethod
    def from_file(
        cls, path: Any, *, fallback: UserInteraction | None = None
    ) -> RecordedUserInteraction:
        import pathlib

        raw = json.loads(pathlib.Path(path).read_text())
        return cls(
            [AskedQuestion.model_validate(row) for row in raw], fallback=fallback
        )


class ElicitationUserInteraction:
    """Asks through an MCP client, using ``elicitation/create``.

    A server never reads stdin — under stdio that descriptor *is* the protocol
    channel — so this is how ``author_workflow`` asks. The MCP client owns the
    UI, which is also what the specification requires: clients "MUST provide UI
    that makes it clear which server is requesting information".

    Capability-gated on purpose. A client that did not declare ``elicitation``
    gets no ``ask_user`` tool, rather than one whose every call fails.
    """

    def __init__(self, context: Any) -> None:
        self._ctx = context

    def available(self) -> bool:
        session = getattr(self._ctx, "session", None)
        capability = getattr(session, "client_params", None)
        declared = getattr(getattr(capability, "capabilities", None), "elicitation", None)
        return declared is not None

    async def ask(self, questions: list[Question]) -> list[Answer]:
        answers: list[Answer] = []
        for question in questions:
            answers.append(await self._one(question))
        return answers

    async def _one(self, question: Question) -> Answer:
        from pydantic import create_model

        fields: dict[str, Any] = {"answer": elicitation_field(question)}
        if question.allow_other and question.choices:
            # The subset has no "enum plus free text", so the escape hatch is a
            # second optional property rather than a widened first one.
            fields["other"] = (str | None, Field(default=None, description="Something else"))
        schema = create_model("Answer", **fields)

        message = question.question
        if question.context:
            message = f"{message}\n\n{question.context}"
        result = await self._ctx.elicit(message=message, schema=schema)

        action = getattr(result, "action", "cancel")
        if action != "accept":
            return Answer(action="decline" if action == "decline" else "cancel")
        data = getattr(result, "data", None)
        other = (getattr(data, "other", None) or "") if data is not None else ""
        raw = getattr(data, "answer", None) if data is not None else None
        if isinstance(raw, list):
            return Answer(action="accept", values=[str(v) for v in raw], other=other)
        if isinstance(raw, bool):
            return Answer(action="accept", values=["yes" if raw else "no"], other=other)
        if raw is None or raw == "":
            return Answer(action="accept", other=other) if other else Answer(action="decline")
        return Answer(action="accept", values=[str(raw)], other=other)


def elicitation_field(question: Question) -> tuple[Any, Any]:
    """*question* as one field of an MCP ``requestedSchema``.

    The restricted subset only — string, number, boolean, enum, and an array of
    enums for multi-select. Nothing nested, because the specification excludes
    it ("complex nested structures … are intentionally not supported") and a
    client is entitled to reject anything richer.

    The enum is emitted through ``json_schema_extra`` rather than built as a
    ``Literal`` of runtime values: that is the shape the spec names, and a
    ``Literal`` over a tuple computed at runtime is not something a type
    checker can read.
    """
    described = " · ".join(
        f"{c.shown()}: {c.description}" for c in question.choices if c.description
    )
    if question.kind == "confirm":
        return (bool | None, Field(default=None, description=question.question))
    if not question.choices:
        return (
            str,
            Field(default=question.default, description=described or question.question),
        )

    values = [c.value for c in question.ordered()]
    labelled = [
        {"const": c.value, "title": c.shown()}
        for c in question.ordered()
        if c.shown() != c.value
    ]
    if question.kind == "multi_select":
        return (
            list[str],
            Field(
                default=None,
                description=described or question.question,
                json_schema_extra=cast(
                    "dict[str, Any]", {"items": {"type": "string", "enum": values}}
                ),
            ),
        )
    # `oneOf` of {const, title} is the spec's labelled-enum form, and it is what
    # puts "Launch SAAS" in front of the person while `PA-1769` comes back.
    extra: dict[str, Any] = {"oneOf": labelled} if labelled else {"enum": values}
    return (
        str,
        Field(
            default=question.default,
            description=described or question.question,
            json_schema_extra=extra,
        ),
    )


class CallbackUserInteraction:
    """Wraps a callable, for tests and for a host with its own transport.

    Accepts a per-question callable as well as a batch one, because a test
    almost always wants "answer whatever is asked" and writing that against a
    list is noise.
    """

    def __init__(
        self,
        callback: Callable[[Any], Awaitable[Any] | Any],
        *,
        available: bool = True,
    ) -> None:
        self._callback = callback
        self._available = available
        self._batched = _takes_a_batch(callback)

    def available(self) -> bool:
        return self._available

    async def ask(self, questions: list[Question]) -> list[Answer]:
        if self._batched:
            result = self._callback(questions)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, list):
                return [_as_answer(one) for one in result]
            return [_as_answer(result)] * len(questions)
        answers: list[Answer] = []
        for question in questions:
            one = self._callback(question)
            if inspect.isawaitable(one):
                one = await one
            answers.append(_as_answer(one))
        return answers


def _takes_a_batch(callback: Any) -> bool:
    """Whether *callback* wants the whole list or one question at a time.

    Read from the annotation rather than discovered by calling, because both
    shapes accept a list without complaining: a per-question callable handed
    the batch returns one answer for four questions, and nothing in the value
    says so. Unannotated means per-question, which is the common case and what
    the single-question API before this did.
    """
    try:
        parameters = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return False
    if not parameters:
        return False
    annotation = parameters[0].annotation
    return "list" in str(annotation)


def _as_answer(value: Any) -> Answer:
    if isinstance(value, Answer):
        return value
    if value is None:
        return Answer(action="decline")
    return Answer(action="accept", other=str(value))


@tool
async def ask_user(questions: list[dict[str, Any]]) -> str:
    """Ask the person who wrote the spec to decide something it leaves open.

    Reserve this for decisions that are genuinely theirs: one you cannot settle
    from the spec, from a lookup, or from a sensible default. Do not ask to
    confirm what the spec already states, do not ask which toolset to use, and
    do not ask anything ``call_read_operation`` would answer — every question
    spends their attention.

    **Ask once.** Gather the open decisions and send up to four in one call
    rather than one per turn.

    Args:
        questions: 1-4 questions. Each is an object with:
            ``question`` (required) - one sentence.
            ``header`` - <= 12 characters, a label for the question.
            ``kind`` - "select" (one of ``choices``), "multi_select",
                "confirm" (yes/no), or "text". Prefer "select": a list is
                answerable in a keystroke and free text is not.
            ``choices`` - up to 8 objects with ``value`` (what you get back),
                ``label`` (what they see), ``description``, and
                ``recommended``. **Mark your preferred choice recommended and
                say why in its description** — it is rendered first and is what
                a declined question falls back to.
            ``context`` - one line on why you are asking.

    Returns:
        One result per question, in order, each with ``action``, ``values`` and
        ``other``. ``accept`` is an answer. ``decline`` means they left it to
        you: use the recommended choice and note it in a comment. ``cancel``
        means nobody answered: do not block on it, take the path you would have
        taken with no ask_user at all.
    """
    return json.dumps({"error": "ask_user is not configured for this agent"})


def make_ask_user_tool(
    interaction: UserInteraction,
    *,
    gate: AskUserGate | None = None,
    record: list[AskedQuestion] | None = None,
) -> Tool:
    """Bind :func:`ask_user` to a :class:`UserInteraction`.

    *record*, when given, accumulates every question and answer, which is what
    ``CodingResult.questions`` is built from and what makes a generation
    reproducible.

    Inside a workflow the answer is journaled via ``ctx.call`` under the
    question's own content hash, so a replay that asks the same thing is not
    re-prompted. Order is deliberately not part of the key: a replayed model
    may ask in a different sequence.
    """
    gate = gate or AskUserGate()

    async def bound(ctx: ToolContext, questions: list[dict[str, Any]]) -> str:
        if not gate.enabled:
            return _refuse(
                "ask_user is not available in this phase — proceed with the "
                "information you have."
            )
        if not questions:
            return _refuse("questions must contain at least one question")
        if len(questions) > MAX_BATCH:
            return _refuse(
                f"ask at most {MAX_BATCH} questions per call; you sent "
                f"{len(questions)}. Send the most important ones."
            )
        remaining = gate.budget - gate.asked
        if remaining <= 0:
            return _refuse(
                f"You have asked {gate.budget} questions already. Proceed with "
                "the information you have."
            )
        if len(questions) > remaining:
            return _refuse(
                f"Only {remaining} of your {gate.budget} questions remain; you "
                f"sent {len(questions)}. Send the most important ones."
            )
        try:
            posed = [_coerce(row) for row in questions]
        except Exception as exc:
            return _refuse(str(exc))

        async def _ask() -> str:
            answers = await interaction.ask(posed)
            gate.asked += len(posed)
            if record is not None:
                record.extend(
                    AskedQuestion(question=q, answer=a)
                    for q, a in zip(posed, answers, strict=False)
                )
            return json.dumps(
                {
                    "answers": [
                        {
                            "question": q.question,
                            "action": a.action,
                            "values": a.values,
                            "other": _sanitize(a.other),
                        }
                        for q, a in zip(posed, answers, strict=False)
                    ]
                }
            )

        if ctx.workflow_ctx is not None:
            digest = hashlib.sha256(
                "|".join(q.id for q in posed).encode("utf-8")
            ).hexdigest()[:16]
            return await ctx.workflow_ctx.call(f"ask_user:{digest}", _ask)
        return await _ask()

    return replace(ask_user, fn=bound, takes_context=True)


def _refuse(reason: str) -> str:
    return json.dumps({"error": reason})


def _coerce(row: Any) -> Question:
    """One question object from the model, validated with a readable failure.

    A model writes ``choices: ["a", "b"]`` about as often as the object form,
    and refusing that costs a turn to say so. Accepting both costs three lines.
    """
    if isinstance(row, Question):
        return row
    if not isinstance(row, dict):
        raise ValueError(f"each question must be an object, got {type(row).__name__}")
    data = dict(row)
    choices = data.get("choices") or []
    data["choices"] = [
        Choice(value=c, label=c) if isinstance(c, str) else c for c in choices
    ]
    if len(data["choices"]) > MAX_CHOICES:
        raise ValueError(f"at most {MAX_CHOICES} choices per question")
    data["header"] = str(data.get("header") or "")[:12]
    if data.get("kind") in (None, "") and data["choices"]:
        data["kind"] = "select"
    return Question.model_validate(data)


def _sanitize(answer: str) -> str:
    return _CTRL.sub("", answer)[:_MAX_ANSWER]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)
