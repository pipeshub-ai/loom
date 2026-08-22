"""Describe a task, and have it happen.

`loom author` wrote a file, registered it, printed a summary and stopped. So
*"find my jira tickets"* — a question whose answer is the tickets — produced
Python and the sentence `loom run my_jira_tickets`. The task **was** the query;
the last step of the loop was missing.

Two decisions, deliberately independent, because conflating them is what makes
either one unpredictable:

* **what gets wired** comes from the triggers the workflow declares. A question
  declares none. "Every weekday at 9" declares a `Schedule`.
* **whether the immediate run asks first** comes from the effect classes the
  *manifests* declare — never from the model's account of its own code.

Both were declarations that already existed and the coding agent had never been
told about: a grep for `triggers=`, `Schedule`, `OnAppEvent` or `@pure` across
`src/loom/agents/` returned nothing at all.
"""

from __future__ import annotations

import pytest

from loom.agents.impact import Verdict, impact_of
from loom.cli.output import Exit
from loom.facade import _declared_triggers, _impact
from loom.runtime.engine import Runtime

READS = '''
from loom import Context, pure, workflow
from loom.toolsets.jira.tools import jira_get_myself, jira_search_issues


@pure
async def format_table(issues: list) -> str:
    return "|".join(str(i) for i in issues)


@workflow(name="my_jira_tickets")
async def my_jira_tickets(ctx: Context, _: None = None) -> str:
    await ctx.step(jira_get_myself)
    found = await ctx.step(jira_search_issues, "assignee = currentUser()")
    return await ctx.step(format_table, found)
'''

WRITES = READS.replace(
    "from loom.toolsets.jira.tools import jira_get_myself, jira_search_issues",
    "from loom.toolsets.jira.tools import (\n"
    "    jira_get_myself,\n    jira_search_issues,\n    jira_transition_issue,\n)",
).replace(
    "    return await ctx.step(format_table, found)",
    "    await ctx.step(jira_transition_issue, 'PA-1', 'Done')\n"
    "    return await ctx.step(format_table, found)",
)


@pytest.fixture
def runtime() -> Runtime:
    return Runtime()


class TestWhatRunningItWouldDo:
    def test_declared_reads_are_read_only(self, runtime: Runtime) -> None:
        found = impact_of(READS, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert found.verdict is Verdict.READ_ONLY
        assert found.safe_to_run
        assert found.writes == ()

    def test_a_write_is_named_rather_than_summarised(self, runtime: Runtime) -> None:
        """"This writes" is not something anybody can act on."""
        found = impact_of(WRITES, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert not found.safe_to_run
        assert [(c.target, c.effect) for c in found.writes] == [
            ("jira_transition_issue", "write")
        ]

    def test_an_unmarked_helper_counts_as_an_effect(self, runtime: Runtime) -> None:
        """`OperationSpec.effect` defaults to WRITE for the same reason.

        Guessing "read" wrong issues a refund nobody was asked about; guessing
        "write" wrong costs one keystroke. `@pure` is how an author says a step
        reaches nothing, rather than this module inferring it from the shape of
        a function body.
        """
        found = impact_of(
            READS.replace("@pure", "@step").replace("pure,", "step,"),
            toolsets=runtime.toolsets,
            nodes=runtime.nodes,
        )
        assert not found.safe_to_run
        assert [c.target for c in found.writes] == ["format_table"]

    def test_an_approval_is_not_a_write(self, runtime: Runtime) -> None:
        """It is the thing that *asks*. Counting it would mean a workflow with a
        human gate needs a second one to reach the first."""
        gated = READS.replace(
            "    found = await ctx.step",
            "    await ctx.wait_for_approval('proceed')\n    found = await ctx.step",
        )
        assert impact_of(gated, toolsets=runtime.toolsets, nodes=runtime.nodes).safe_to_run

    def test_a_model_call_is_not_a_write(self, runtime: Runtime) -> None:
        judged = READS.replace(
            "    return await ctx.step(format_table, found)",
            "    await ctx.agent('which of these is urgent?')\n"
            "    return await ctx.step(format_table, found)",
        )
        assert impact_of(judged, toolsets=runtime.toolsets, nodes=runtime.nodes).safe_to_run

    def test_unparseable_code_is_never_reported_harmless(self, runtime: Runtime) -> None:
        found = impact_of("def (:", toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert not found.safe_to_run

    def test_no_registry_means_nothing_is_a_declared_read(self) -> None:
        """The fail-safe direction: with nothing to ask, everything asks."""
        assert not impact_of(READS).safe_to_run

    def test_it_reads_the_catalogue_not_the_broker_lookup(self, runtime: Runtime) -> None:
        """`effect_of` is execution scope and answers `None` on a bare Runtime.

        That carve-out is deliberate — widening the broker's per-dispatch
        lookup would reclassify steps in deployments that registered no toolset
        — but this is a declaration being read back to a person before they
        press a key, so it reads the catalogue instead.
        """
        assert runtime.toolsets.effect_of("jira_search_issues") is None
        assert impact_of(READS, toolsets=runtime.toolsets).safe_to_run


class TestTheDeclaredTrigger:
    """Read from the source, before anything has imported it.

    Importing model-written code to find out whether it is safe to run has the
    order backwards.
    """

    def test_a_question_declares_nothing(self) -> None:
        assert _declared_triggers(READS) == []

    def test_a_schedule_is_read_with_its_cron_and_zone(self) -> None:
        source = READS.replace(
            '@workflow(name="my_jira_tickets")',
            '@workflow(name="my_jira_tickets",\n'
            '          triggers=[Schedule(cron="0 9 * * 1-5", timezone="Asia/Kolkata")])',
        )
        assert _declared_triggers(source) == [
            {
                "kind": "Schedule",
                "fields": {"cron": "0 9 * * 1-5", "timezone": "Asia/Kolkata"},
                "args": [],
            }
        ]

    def test_an_app_event_carries_its_topic(self) -> None:
        source = READS.replace(
            '@workflow(name="my_jira_tickets")',
            '@workflow(name="my_jira_tickets", triggers=[OnAppEvent("app.slack.message")])',
        )
        assert _declared_triggers(source) == [
            {"kind": "OnAppEvent", "fields": {}, "args": ["app.slack.message"]}
        ]

    def test_it_survives_code_that_does_not_parse(self) -> None:
        assert _declared_triggers("def (:") == []


class TestTheAuthorPayload:
    """Flattened onto the port, so every surface decides the same way."""

    def test_it_carries_the_verdict_and_the_writes(self, runtime: Runtime) -> None:
        payload = _impact(WRITES, runtime)
        assert payload["impact"] == "effectful"
        assert payload["writes"][0]["target"] == "jira_transition_issue"
        assert payload["writes"][0]["effect"] == "write"

    def test_a_read_only_workflow_says_so(self, runtime: Runtime) -> None:
        assert _impact(READS, runtime)["impact"] == "read_only"

    def test_no_code_is_empty_rather_than_effectful(self, runtime: Runtime) -> None:
        assert _impact("", runtime)["impact"] == "empty"

    def test_a_broken_registry_fails_safe(self) -> None:
        """Never fail an authoring run over the summary of it — but an unknown
        verdict must be the one that asks."""

        class Broken:
            @property
            def toolsets(self) -> object:
                raise RuntimeError("no registry here")

        assert _impact(READS, Broken())["impact"] == "effectful"


class TestTheAgentIsToldAboutBoth:
    """The gap was never the machinery. `@workflow(triggers=[...])` and `@pure`
    both shipped long ago; a grep across `src/loom/agents/` for either returned
    nothing, so the model had no way to know they existed."""

    @pytest.mark.parametrize(
        "declaration",
        ["triggers=", "Schedule(", "Interval(", "OnAppEvent(", "@pure"],
    )
    def test_the_prompt_names_it(self, declaration: str) -> None:
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert declaration in DEFAULT_SYSTEM_PROMPT

    def test_it_is_told_not_to_invent_a_schedule(self) -> None:
        """A workflow that runs hourly because the spec *sounded* periodic is
        one nobody asked to run at all."""
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "invent" in DEFAULT_SYSTEM_PROMPT
        assert "sounded periodic" in DEFAULT_SYSTEM_PROMPT


class TestTheSurfaceActsOnIt:
    def test_the_session_asks_for_a_run_and_the_cli_does_not(self) -> None:
        """A line typed at the session prompt is a task. `loom author "spec" >
        flow.py` is documented and must keep putting *code* in the file."""
        import inspect

        from loom.cli.repl.session import Session

        assert '"--run"' in inspect.getsource(Session._prose)

    def test_the_flag_exists_and_defaults_off(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(["author", "do a thing"])
        assert args.run is False
        assert build_parser().parse_args(["author", "x", "--run"]).run is True

    async def test_findings_stop_a_run(self) -> None:
        """Unresolved findings are a review, not a failure — and also not a
        reason to reach a real API on somebody's behalf."""
        import argparse

        from loom.cli.commands import _may_run
        from loom.cli.output import Printer

        out = Printer(as_json=True, quiet=True)
        args = argparse.Namespace(run=True, yes=False)
        assert await _may_run(out, {"clean": False, "impact": "read_only"}, args) is False

    async def test_reads_run_without_asking(self) -> None:
        import argparse

        from loom.cli.commands import _may_run
        from loom.cli.output import Printer

        out = Printer(as_json=True, quiet=True)
        args = argparse.Namespace(run=True, yes=False)
        assert await _may_run(out, {"clean": True, "impact": "read_only", "writes": []}, args)

    async def test_a_write_is_refused_when_nothing_can_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule `before` hooks and `propose` already follow: a gate that
        could not run has not passed."""
        import argparse
        import sys

        from loom.cli.commands import _may_run
        from loom.cli.output import Printer

        monkeypatch.setattr(
            sys, "stdin", type("NoTTY", (), {"isatty": lambda self: False})()
        )
        out = Printer(as_json=True, quiet=True)
        args = argparse.Namespace(run=True, yes=False)
        result = {
            "clean": True,
            "impact": "effectful",
            "writes": [{"target": "jira_transition_issue", "effect": "write"}],
        }
        assert await _may_run(out, result, args) is False

    async def test_yes_is_the_override(self) -> None:
        import argparse

        from loom.cli.commands import _may_run
        from loom.cli.output import Printer

        out = Printer(as_json=True, quiet=True)
        args = argparse.Namespace(run=True, yes=True)
        result = {
            "clean": True,
            "impact": "effectful",
            "writes": [{"target": "jira_transition_issue", "effect": "write"}],
        }
        assert await _may_run(out, result, args) is True

    async def test_nothing_runs_when_nobody_asked(self) -> None:
        import argparse

        from loom.cli.commands import _may_run
        from loom.cli.output import Printer

        out = Printer(as_json=True, quiet=True)
        assert await _may_run(out, {"clean": True}, argparse.Namespace(run=False)) is None

    def test_the_workflow_name_comes_from_the_source(self) -> None:
        from loom.cli.commands import _authored_name

        assert _authored_name({"code": READS}) == "my_jira_tickets"
        assert _authored_name({"code": "def (:"}) is None


class TestActingHappensInsideTheCommandsOwnLoop:
    """`cmd_author` is already running under `run_async`.

    The first version drove the run with a second `run_async`, which is
    `asyncio.run()` inside a running loop — so the whole thing raised
    *"asyncio.run() cannot be called from a running event loop"* **after** the
    file had been written and the summary printed, which is the worst place to
    fail. Every unit test passed: they called `_may_run` directly and never
    exercised the nesting, which is the shape `tests/test_ask_user_wiring.py`
    exists for.
    """

    def test_the_acting_path_is_awaited_not_driven(self) -> None:
        import inspect

        from loom.cli import commands

        for name in ("_act_on", "_run_now", "_may_run"):
            fn = getattr(commands, name)
            assert inspect.iscoroutinefunction(fn), f"{name} must be awaited"
            assert "run_async(" not in inspect.getsource(fn), (
                f"{name} opens a second event loop inside the command's own"
            )

    @pytest.mark.asyncio
    async def test_it_runs_under_a_loop_without_raising(self) -> None:
        """The regression, reproduced: call it from inside a running loop."""
        import argparse

        from loom.cli.commands import _act_on
        from loom.cli.output import Printer

        out = Printer(as_json=True, quiet=True)
        args = argparse.Namespace(run=True, yes=False)
        result = {
            "clean": True,
            "impact": "read_only",
            "writes": [],
            "code": READS,
            "triggers": [{"kind": "Schedule", "fields": {"cron": "0 9 * * *"},
                          "args": []}],
        }

        # `run=False`, so it reports the trigger and stops before starting a
        # run — the nesting is what is under test, not the Runtime.
        quiet = argparse.Namespace(run=False, yes=False)
        assert await _act_on(out, None, result, quiet) == Exit.OK

        # And the decision path itself, which is where `input()` would have
        # blocked the loop had it not moved to a thread.
        assert await _may_run_for(out, result, args) is True


async def _may_run_for(out: object, result: dict, args: object) -> bool | None:
    from loom.cli.commands import _may_run

    return await _may_run(out, result, args)  # type: ignore[arg-type]


class TestTheWholeCommandRunsIt:
    """`cmd_author` end to end, with a fake facade.

    The unit tests all passed while the command raised
    *"asyncio.run() cannot be called from a running event loop"* on every
    invocation — because they called the pieces and nothing called the command.
    This drives `cmd_author` itself, which is the only shape that would have
    caught it.
    """

    def _target(self, tmp_path, monkeypatch, *, code: str, started: list) -> None:
        import argparse

        from loom.cli import commands

        class FakeBackend:
            async def author(self, *a: object, **k: object) -> dict:
                from loom.facade import _impact

                return {
                    "code": code, "clean": True, "repairs": 0, "model": "fake",
                    "input_tokens": 1, "output_tokens": 1, "issues": [],
                    "plan": [], "tools_used": [], "questions": [],
                    "session_id": "s", "smoke": {"ok": True}, **_impact(code, Runtime()),
                }

            async def start(self, name: str, payload: object, **k: object) -> dict:
                started.append(name)
                return {
                    "run_id": "r1", "workflow": name, "status": "completed",
                    "output": "PA-1769  Launch SAAS", "error": None,
                }

            async def close(self) -> None:
                return None

        class FakeTarget:
            backend = FakeBackend()
            project = None
            workflow = None

        monkeypatch.setattr(commands, "with_backend", lambda *a, **k: FakeTarget())
        monkeypatch.setattr(commands, "_register", lambda *a, **k: None)
        return argparse.Namespace(
            spec="find my tickets", output=tmp_path / "flow.py", package=None,
            input=None, no_observe=True, turns=None, max_tokens=None,
            max_cost=None, resume="", json=False, quiet=True, debug=False,
            yes=True, run=True, server=None, module=None, store=None,
            save_answers=None, answers=None, no_ask=True, verbose=False,
        )

    def test_a_read_only_workflow_is_written_and_run(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.cli.commands import cmd_author

        started: list[str] = []
        args = self._target(tmp_path, monkeypatch, code=READS, started=started)

        assert cmd_author(args) == Exit.OK
        assert started == ["my_jira_tickets"], "the task was the query; it must run"
        assert (tmp_path / "flow.py").exists()

    def test_a_writing_workflow_is_not_run_unattended(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--yes` is the write override, and a non-TTY refuses without it."""
        import sys

        from loom.cli.commands import cmd_author

        started: list[str] = []
        args = self._target(tmp_path, monkeypatch, code=WRITES, started=started)
        args.yes = False
        monkeypatch.setattr(
            sys, "stdin", type("NoTTY", (), {"isatty": lambda self: False})()
        )

        assert cmd_author(args) == Exit.OK
        assert started == [], "a write ran with nobody able to answer"

    def test_nothing_runs_when_run_was_not_asked_for(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`loom author "spec" > flow.py` must keep putting code in the file."""
        from loom.cli.commands import cmd_author

        started: list[str] = []
        args = self._target(tmp_path, monkeypatch, code=READS, started=started)
        args.run = False

        assert cmd_author(args) == Exit.OK
        assert started == []


WRAPPED = '''
from loom import Context, pure, step, workflow
from loom.toolsets.jira.tools import (
    jira_delete_issue,
    jira_resolve_project,
    jira_search_issues,
)


@step
async def resolve_project(name: str) -> str:
    found = await jira_resolve_project(project_name=name)
    return found.matches[0].key


@step
async def search_past_due(key: str) -> list:
    return await jira_search_issues(f"project = {key} AND duedate < now()")


@step
async def purge(key: str) -> None:
    await jira_delete_issue(key)


@pure
async def format_table(rows: list) -> str:
    return "x"


@workflow(name="w")
async def w(ctx: Context, name: str = "saas") -> str:
    key = await ctx.step(resolve_project, name)
    rows = await ctx.step(search_past_due, key)
    return await ctx.step(format_table, rows)
'''


class TestItSeesThroughAWrapper:
    """The agent almost always writes one.

    The prompt tells it to put I/O inside a step, so the *durable call* names
    `resolve_project` and the toolset call is one level down. Reading only the
    call site made every generated workflow "unclassified", which asks about a
    workflow whose whole content is two reads — and an ask nobody can act on is
    an ask people learn to answer without reading.
    """

    def test_wrapped_reads_are_read_only(self, runtime: Runtime) -> None:
        found = impact_of(WRAPPED, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert found.verdict is Verdict.READ_ONLY, [
            (c.target, c.effect) for c in found.writes
        ]

    def test_a_wrapped_delete_still_surfaces(self, runtime: Runtime) -> None:
        code = WRAPPED.replace(
            "    rows = await ctx.step(search_past_due, key)",
            "    rows = await ctx.step(search_past_due, key)\n"
            "    await ctx.step(purge, key)",
        )
        found = impact_of(code, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert [(c.target, c.effect) for c in found.writes] == [
            ("purge", "destructive")
        ]

    def test_a_wrapper_doing_two_things_is_not_resolved(
        self, runtime: Runtime
    ) -> None:
        """One level, and only when every call inside agrees.

        A wrapper that reads *and* writes is the write; resolving it to either
        would be inference rather than the certain question this answers.
        """
        code = WRAPPED.replace(
            "async def search_past_due(key: str) -> list:\n"
            "    return await jira_search_issues(f\"project = {key} AND duedate < now()\")",
            "async def search_past_due(key: str) -> list:\n"
            "    await jira_delete_issue(key)\n"
            "    return await jira_search_issues(key)",
        )
        found = impact_of(code, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert not found.safe_to_run

    def test_a_wrapper_calling_something_undeclared_stays_unclassified(
        self, runtime: Runtime
    ) -> None:
        code = WRAPPED.replace(
            "    return await jira_search_issues(f\"project = {key} AND duedate < now()\")",
            "    await some_other_api(key)\n    return await jira_search_issues(key)",
        )
        found = impact_of(code, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert not found.safe_to_run

    def test_builtins_do_not_make_a_wrapper_suspicious(
        self, runtime: Runtime
    ) -> None:
        """Otherwise every wrapper is unclassified, which is the same as not
        having this at all — `len()` is not undeclared I/O."""
        code = WRAPPED.replace(
            "    return await jira_search_issues(f\"project = {key} AND duedate < now()\")",
            "    rows = await jira_search_issues(key)\n    return list(rows)[: len(rows)]",
        )
        found = impact_of(code, toolsets=runtime.toolsets, nodes=runtime.nodes)
        assert found.safe_to_run
