"""Every surface that authors code can ask the person who asked for it.

``interaction.py`` shipped complete — a protocol, a CLI implementation that
writes to stderr and degrades on a non-TTY, a per-generation budget, and a
tool factory that journals the answer inside a workflow. ``tests/test_interaction.py``
covers all of it.

**Nothing passed it in.** ``WorkflowCodingAgent`` took ``user_interaction=``
and no caller in the repository supplied one, so ``CLIUserInteraction`` was
unreachable code and ``ask_user`` was a tool no model was ever offered. A
capability nobody wires is indistinguishable from one nobody wrote, and the
module's own tests could not tell the difference — they construct the agent
themselves.

So these are wiring tests, deliberately at the seams rather than at the unit:
what the CLI builds, what a server builds, and what the shipped examples pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from loom import Runtime
from loom.agents.interaction import (
    Answer,
    CLIUserInteraction,
    Question,
    UserInteraction,
)
from loom.facade import LocalFacade

COOKBOOK = Path(__file__).resolve().parents[1] / "examples" / "cookbook"

#: Examples whose job is to author a workflow for whoever ran them.
#:
#: ``10_langchain_react_agent.py`` is deliberately absent: it times two
#: frameworks against the same spec, and a prompt in the middle would be
#: measured as latency.
AUTHORING_EXAMPLES = [
    "07_coding_agent.py",
    "08_jira_agent.py",
    "09_jira_cli.py",
    "14_workflow_manager_cli.py",
    "18_gmail_calendar.py",
]


class TestTheDefaultIsUnchanged:
    """Absence degrades to exactly what shipped before it existed."""

    def test_a_bare_local_facade_has_none(self) -> None:
        assert LocalFacade(Runtime()).user_interaction is None

    def test_no_interaction_means_no_tool_and_no_prompt_section(self) -> None:
        """``build_coding_tools`` omits ``ask_user`` rather than offering one
        that always answers "not configured" — the rule ``observe_target``
        follows, and for the same reason: a tool a model can see is a tool it
        will spend a turn on."""
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent(model=None)

        assert agent._ask_gate is None
        assert "ask_user" not in agent.build_system_prompt()


class TestTheCliWiresIt:
    """The one surface with a person at the other end of stdin."""

    def test_resolve_builds_a_facade_that_can_ask(self) -> None:
        """What the CLI *wires*, which is independent of what it loads.

        ``modules=[]`` and ``store="memory://"`` on purpose. Without them this
        resolved the repository's own ``pyproject.toml`` and imported whatever
        ``[tool.loom] modules`` happened to name — so authoring two workflows
        that share a name, which ``loom author`` makes easy since it names a
        file from the spec, failed this test with
        ``a different workflow named '...' is registered``. A wiring test that
        a developer's own working directory can break reports its own
        environment, and the failure names neither.
        """
        from loom.cli import targets

        target = targets.resolve(None, modules=[], store="memory://")

        assert isinstance(target.backend, LocalFacade)
        assert isinstance(target.backend.user_interaction, CLIUserInteraction)
        assert isinstance(target.backend.user_interaction, UserInteraction)

    async def test_the_agent_it_builds_receives_it(self) -> None:
        """The field is inert unless ``_coding_agent`` passes it on, which is
        the exact link that was missing."""
        pytest.importorskip("anthropic", reason="needs a provider to build an agent")
        from loom.agents import providers
        from loom.agents.interaction import Answer, CallbackUserInteraction

        if providers.from_env() is None:
            pytest.skip("no provider key configured")

        facade = LocalFacade(
            Runtime(),
            [],
            user_interaction=CallbackUserInteraction(
                lambda q: Answer(action="accept", other="x")
            ),
        )
        agent = await facade._coding_agent(
            packages=None, observe=False, smoke_input=None
        )

        assert agent._ask_gate is not None

    async def test_a_cli_interaction_with_no_tty_is_dropped(self) -> None:
        """The capability check, at the seam where it matters.

        Under pytest — and under any pipe, and in CI — stdin is not a terminal,
        so the CLI's interaction reports itself unavailable and the agent is
        built without ``ask_user`` at all. That is the whole point of checking a
        capability instead of waiting for a timeout: the question is never
        asked rather than asked and abandoned five minutes later.
        """
        pytest.importorskip("anthropic", reason="needs a provider to build an agent")
        from loom.agents import providers

        if providers.from_env() is None:
            pytest.skip("no provider key configured")

        facade = LocalFacade(Runtime(), [], user_interaction=CLIUserInteraction())
        agent = await facade._coding_agent(
            packages=None, observe=False, smoke_input=None
        )

        assert CLIUserInteraction().available() is False
        assert agent._ask_gate is None
        assert "ask_user" not in agent.build_system_prompt()


class TestAServerDoesNotWireIt:
    """There is no human on a server's stdin, and under ``loom mcp
    --transport stdio`` that file descriptor *is* the protocol channel."""

    def test_the_mcp_facade_cannot_ask(self) -> None:
        """A facade built without one has none.

        This used to go through ``RuntimeBridge``, which is gone; the claim was
        never about the bridge, it was that nothing gives a server an
        interaction unless somebody composes one in — which is the CLI's job
        and not a library's.
        """
        assert LocalFacade(Runtime()).user_interaction is None

    def test_the_remote_facade_has_no_such_field(self) -> None:
        """An interaction is an object, not a payload, so it could not cross
        the wire — which is consistent with ``RemoteFacade.author`` refusing
        outright, because authoring runs where the code will run."""
        from loom.facade import RemoteFacade

        assert not hasattr(RemoteFacade, "user_interaction")


@pytest.mark.parametrize("name", AUTHORING_EXAMPLES)
class TestTheExamplesWireIt:
    def test_the_example_passes_an_interaction(self, name: str) -> None:
        source = (COOKBOOK / name).read_text()

        assert "user_interaction=" in source, (
            f"{name} authors a workflow but never lets the agent ask a "
            "question — the state this file exists to catch"
        )
        assert "CLIUserInteraction" in source

    def test_it_is_passed_where_the_agent_is_built(self, name: str) -> None:
        """Present in the file is not the same as reaching the agent.

        Read from the syntax rather than by grepping for adjacency, because
        the two lines drifting apart is how the original defect looked from
        the outside: the import was there, the constructor was not.
        """
        tree = ast.parse((COOKBOOK / name).read_text())

        built = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WorkflowCodingAgent"
        ]
        assert built, f"{name} is listed as an authoring example but builds no agent"
        for call in built:
            assert "user_interaction" in {kw.arg for kw in call.keywords}


class TestTheLadderChangesWhenAskingIsPossible:
    """Rung 3 splits, and only when there is somebody to ask.

    An ambiguity in the *spec* has one answer that does not change between
    runs, so it is a question for a person and the answer is baked in. An
    ambiguity in *runtime input* cannot be answered at authoring time at all,
    and that is what ``ctx.agent()`` is for. Saying so unconditionally would
    instruct a tool that is not there.
    """

    @staticmethod
    def _prompt(wired: bool) -> str:
        from loom.agents.coding_agent import WorkflowCodingAgent
        from loom.agents.interaction import CallbackUserInteraction

        async def answer(question):  # pragma: no cover - never called
            return ""

        agent = WorkflowCodingAgent(
            model=None,
            user_interaction=CallbackUserInteraction(answer) if wired else None,
        )
        return agent.build_system_prompt()

    def test_it_appears_only_when_wired(self) -> None:
        assert "Asking the user" not in self._prompt(False)
        assert "Asking the user" in self._prompt(True)

    def test_it_distinguishes_a_spec_ambiguity_from_a_runtime_one(self) -> None:
        wired = self._prompt(True)

        assert "came from the spec" in wired
        assert "arrives as workflow input" in wired
        assert "ctx.agent()" in wired

    def test_each_of_the_three_answers_has_an_instruction(self) -> None:
        """``decline`` and ``cancel`` want opposite things — take the
        recommendation, versus defer the decision to run time — so a prompt
        that named only one of them would leave the model guessing at the
        other, which is the state the two-outcome model was in."""
        wired = self._prompt(True)

        for action in ("`accept`", "`decline`", "`cancel`"):
            assert action in wired

    def test_it_asks_for_a_recommendation_and_a_batch(self) -> None:
        wired = self._prompt(True)

        assert "recommended" in wired
        assert "four questions" in wired


class TestTheAnswersAreABuildInput:
    """``--save-answers`` / ``--answers``, and why they exist.

    Every other agent in this space treats a clarifying answer as conversation
    — said once, kept nowhere. That is the one thing this repository does not
    do with anything else: a workflow authored with human answers could not be
    regenerated, so ``loom author`` could not run in CI at all once the agent
    started asking, and "why does this filter on that id?" had no answer but
    the person who ran it.
    """

    @staticmethod
    def _args(**kw):
        import argparse

        return argparse.Namespace(
            **{"no_ask": False, "answers": None, "save_answers": None, **kw}
        )

    @staticmethod
    def _target(interaction=None):
        from loom.cli.targets import Target

        return Target(backend=LocalFacade(Runtime(), [], user_interaction=interaction))

    def test_no_ask_removes_the_interaction(self) -> None:
        from loom.cli.commands import _asking

        target = self._target(CLIUserInteraction())
        _asking(self._args(no_ask=True), target)

        assert target.backend.user_interaction is None

    def test_answers_wrap_the_terminal_rather_than_replacing_it(
        self, tmp_path
    ) -> None:
        """A spec that grew a new ambiguity should ask about that one, not
        re-ask everything and not refuse to build."""
        from loom.agents.interaction import RecordedUserInteraction
        from loom.cli.commands import _asking

        path = tmp_path / "answers.json"
        path.write_text("[]")
        cli = CLIUserInteraction()
        target = self._target(cli)

        _asking(self._args(answers=path), target)

        played = target.backend.user_interaction
        assert isinstance(played, RecordedUserInteraction)
        assert played._fallback is cli

    def test_save_answers_writes_a_file_the_replay_can_read(self, tmp_path) -> None:
        from loom.agents.interaction import AskedQuestion, RecordedUserInteraction
        from loom.cli.commands import _save_answers

        asked = AskedQuestion(
            question=Question(question="Which epic?", kind="select"),
            answer=Answer(action="accept", values=["PA-1769"]),
        )
        path = tmp_path / "answers.json"

        _save_answers(
            self._args(save_answers=path),
            {"questions": [asked.model_dump(mode="json")]},
        )

        # Round trip through the file, not merely written: the claim is that a
        # later run can replay it, and a file only this test can parse would
        # satisfy an assertion about its existence.
        played = RecordedUserInteraction.from_file(path)
        assert played.available() is True

    def test_nothing_is_written_when_nothing_was_asked(self, tmp_path) -> None:
        """An empty file reads as "this run asked nothing" only if you know the
        flag was passed, and as "the flag did not work" otherwise."""
        from loom.cli.commands import _save_answers

        path = tmp_path / "answers.json"
        _save_answers(self._args(save_answers=path), {"questions": []})

        assert not path.exists()

    def test_the_flags_exist_on_both_authoring_commands(self) -> None:
        from loom.cli import build_parser

        parser = build_parser()
        for argv in (["author", "spec"], ["edit", "flow.py", "change it"]):
            args = parser.parse_args([*argv, "--no-ask"])

            assert args.no_ask is True
            assert hasattr(args, "answers") and hasattr(args, "save_answers")
