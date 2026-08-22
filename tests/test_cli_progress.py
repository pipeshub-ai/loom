"""Making an authoring run visible while it runs.

``loom author`` awaited one coroutine and printed nothing until it returned.
Behind that await: twenty discovery turns, sixteen verification stages, three
repair rounds each re-invoking the model, a subprocess smoke run and a replay.
The seventeen ``logger.info`` calls narrating it went to a logger no CLI
configures.

Most of this file tests **wiring**, not rendering, and that is deliberate. The
renderer, the hook family and the pipeline callback were each complete and
correct on their own while ``generate()`` constructed its ``CodingSession``
without a registry — so the whole feature was dead at one keyword argument, and
nothing that tested a piece in isolation could have said so. It is the same
defect ``test_ask_user_wiring`` was written for: a capability nobody wires is
indistinguishable from one nobody wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.messages import ToolCall
from loom.cli.output import Exit
from loom.cli.progress import ProgressRenderer, brief_arguments
from loom.runtime.hooks import HookRegistry
from loom.testing.mock import MockModelProvider, mock_response

WORKFLOW = (
    "from loom import Context, workflow\n\n\n"
    '@workflow(name="progressed")\n'
    'async def progressed(ctx: Context, _: str = "") -> str:\n'
    '    """Something that compiles."""\n'
    '    return "ok"\n'
)

AGENT_EVENTS = (
    "agent_start",
    "turn_start",
    "model_start",
    "model_end",
    "tool_start",
    "tool_end",
    "turn_end",
    "agent_end",
)


def recording_registry() -> tuple[HookRegistry, list[tuple[str, str]]]:
    """A registry that records every agent event, with the tool it names."""
    seen: list[tuple[str, str]] = []
    hooks = HookRegistry()

    def wire(event: str) -> None:
        async def observe(ctx: Any) -> None:
            seen.append((event, str(getattr(ctx, "tool", "") or "")))

        observe.__name__ = event
        getattr(hooks, f"on_{event}")(observe)

    for event in AGENT_EVENTS:
        wire(event)
    return hooks, seen


def scripted(*, tool: str | None = None) -> MockModelProvider:
    """A model that optionally calls one tool, then returns a workflow."""
    responses = []
    if tool is not None:
        responses.append(
            mock_response(
                tool_calls=[ToolCall(id="1", name=tool, arguments={"query": "jira"})]
            )
        )
    responses.append(
        mock_response(
            tool_calls=[
                ToolCall(
                    id="2",
                    name="final_output",
                    arguments={
                        "code": WORKFLOW,
                        "explanation": "done",
                        "plan": [],
                    },
                )
            ]
        )
    )
    return MockModelProvider(responses=responses)


class TestTheHooksAreActuallyWired:
    """The defect: every piece worked and nothing connected them.

    ``WorkflowCodingAgent(hooks=…)`` stored the registry, ``CodingSession``
    forwarded it, and ``generate`` constructed its session without it — so a
    caller that passed a registry got no events at all, and only an end-to-end
    assertion says so.
    """

    async def test_generate_reaches_the_hooks(self) -> None:
        hooks, seen = recording_registry()
        agent = WorkflowCodingAgent(
            scripted(tool="search_toolsets"), hooks=hooks, smoke_test=False
        )
        result = await agent.generate("do a thing")

        assert result.code
        assert ("agent_start", "") in seen
        assert ("tool_start", "search_toolsets") in seen
        assert ("tool_end", "search_toolsets") in seen
        assert ("agent_end", "") in seen

    async def test_edit_reaches_the_hooks(self) -> None:
        """Both entry points, because they build their sessions separately."""
        hooks, seen = recording_registry()
        agent = WorkflowCodingAgent(
            scripted(tool="search_toolsets"), hooks=hooks, smoke_test=False
        )
        await agent.edit(WORKFLOW, "change something")
        assert ("tool_start", "search_toolsets") in seen

    async def test_no_hooks_is_the_behaviour_that_shipped(self) -> None:
        """An agent given no registry runs exactly as it did before."""
        agent = WorkflowCodingAgent(scripted(), smoke_test=False)
        assert (await agent.generate("do a thing")).code


class TestToolEventsArePaired:
    """Every ``tool_start`` gets a ``tool_end``.

    A start with no end leaves a live region describing a call that finished —
    so the two early returns in ``_one_tool_call`` have to close the pair too.
    """

    async def _events(self, tool: str) -> list[tuple[str, str]]:
        hooks, seen = recording_registry()
        agent = WorkflowCodingAgent(
            scripted(tool=tool), hooks=hooks, smoke_test=False
        )
        await agent.generate("do a thing")
        return seen

    async def test_a_normal_call(self) -> None:
        seen = await self._events("search_toolsets")
        assert sum(1 for e, _ in seen if e == "tool_start") == sum(
            1 for e, _ in seen if e == "tool_end"
        )

    async def test_an_unknown_tool_still_closes(self) -> None:
        """The model asking for a tool that is not there is the common case."""
        seen = await self._events("no_such_tool_at_all")
        starts = [t for e, t in seen if e == "tool_start"]
        ends = [t for e, t in seen if e == "tool_end"]
        assert starts == ends == ["no_such_tool_at_all"]

    async def test_the_outcome_is_reported(self) -> None:
        outcomes: list[str] = []
        hooks = HookRegistry()

        async def on_end(ctx: Any) -> None:
            outcomes.append(str(getattr(ctx, "outcome", "")))

        hooks.on_tool_end(on_end)
        agent = WorkflowCodingAgent(
            scripted(tool="no_such_tool_at_all"), hooks=hooks, smoke_test=False
        )
        await agent.generate("do a thing")
        assert outcomes == ["unknown"]


class TestPipelineAnnouncesStages:
    async def test_each_stage_opens_and_closes(self) -> None:
        from loom.agents.checks import CheckContext, CheckPipeline, CheckResult

        class Stage:
            name = "example"
            cost = 1
            blocking = False

            async def run(self, _code: str, _context: Any) -> CheckResult:
                return CheckResult(name=self.name)

        seen: list[tuple[str, bool]] = []

        def on_stage(check: Any, result: Any) -> None:
            seen.append((check.name, result is not None))

        await CheckPipeline([Stage()]).run(
            "code", CheckContext(spec=""), on_stage=on_stage
        )
        assert seen == [("example", False), ("example", True)]

    async def test_a_throwing_callback_cannot_break_the_pipeline(self) -> None:
        """A renderer must never be able to fail a clean generation."""
        from loom.agents.checks import CheckContext, CheckPipeline, CheckResult

        class Stage:
            name = "example"
            cost = 1
            blocking = False

            async def run(self, _code: str, _context: Any) -> CheckResult:
                return CheckResult(name=self.name)

        def explode(*_: Any) -> None:
            raise RuntimeError("the renderer is broken")

        report = await CheckPipeline([Stage()]).run(
            "code", CheckContext(spec=""), on_stage=explode
        )
        assert [r.name for r in report.results] == ["example"]

    async def test_no_callback_is_the_old_signature(self) -> None:
        from loom.agents.checks import CheckContext, CheckPipeline

        assert (await CheckPipeline([]).run("code", CheckContext(spec=""))).results == []


class TestBriefArguments:
    """A tool call is a headline. Three lines of JSON schema is not one."""

    def test_a_single_string_argument_is_bare(self) -> None:
        assert brief_arguments({"query": "jira"}) == '"jira"'

    def test_several_are_named(self) -> None:
        rendered = brief_arguments({"a": "one", "b": 2})
        assert "a=" in rendered and "b=" in rendered

    def test_long_values_are_cut_individually(self) -> None:
        """So a call with one huge argument still shows the others' names."""
        rendered = brief_arguments({"code": "x" * 500, "name": "keep"})
        assert len(rendered) < 200
        assert "name=" in rendered

    def test_nothing_renders_as_nothing(self) -> None:
        assert brief_arguments({}) == ""
        assert brief_arguments(None) == ""
        assert brief_arguments("not a dict") == ""


async def _noop(_: Any) -> None:
    """Stand-in for an event this renderer does not draw."""


class TestTheClockMoves:
    """The longest gap between events is a model call.

    ``Live`` re-renders whatever object it holds, so it was handed a finished
    ``Text`` and redrew the same frame eight times a second — twenty frames
    over two and a half seconds of silence, every one of them reading "0s".
    Which is exactly the stretch during which somebody is wondering whether it
    has hung, and the reason the live region exists at all.
    """

    def _rendered(self, seconds: float) -> str:
        import io
        import time

        pytest.importorskip("rich")
        from rich.console import Console

        buffer = io.StringIO()
        renderer = ProgressRenderer(
            enabled=True,
            live_capable=True,
            _console=Console(file=buffer, force_terminal=True, width=80),
        )
        renderer._begin("thinking")
        time.sleep(seconds)
        renderer.close()
        return buffer.getvalue()

    def test_elapsed_advances_with_no_events_at_all(self) -> None:
        import re

        shown = set(re.findall(r"· (\d+)s", self._rendered(2.2)))
        assert len(shown) >= 2, (
            f"the live region drew only {shown or {'nothing'}} across 2.2s of "
            "silence — the clock is frozen between events"
        )

    def test_it_draws_more_than_once(self) -> None:
        """Guards the test above: one frame would trivially show one value."""
        assert self._rendered(1.2).count("thinking") > 1


class TestRendererIsSafe:
    """It observes. It must not be able to change anything."""

    def test_disabled_registers_nothing(self) -> None:
        hooks = HookRegistry()
        ProgressRenderer(enabled=False).install(hooks)
        assert hooks.has_agent is False

    def test_json_mode_produces_a_disabled_renderer(self) -> None:
        assert ProgressRenderer.for_terminal(enabled=False).enabled is False

    async def test_every_callback_survives_a_junk_context(self) -> None:
        """Hooks fail open, so a renderer that raises would be silently useless."""

        class Nothing:
            pass

        renderer = ProgressRenderer(enabled=True, live_capable=False)
        # Dispatched through a registry, the way the runner does it, so the set
        # under test is whatever the renderer actually registers rather than a
        # list here that can drift from it.
        hooks = HookRegistry()
        renderer.install(hooks)
        from loom.runtime.hooks import AgentHookContext

        for event in ProgressRenderer.HANDLES:
            await hooks.dispatch_agent(event, AgentHookContext())
        for event in AGENT_EVENTS:
            await getattr(renderer, f"_{event}", _noop)(Nothing())
        await renderer.stage(Nothing(), None)
        await renderer.stage(Nothing(), Nothing())
        renderer.flush_stages()
        renderer.note("something happened")
        renderer.close()
        renderer.close()

    def test_close_is_idempotent(self) -> None:
        renderer = ProgressRenderer.for_terminal()
        renderer.close()
        renderer.close()


FLOWS_PYPROJECT = """
[project]
name = "progresstest"
version = "0.1.0"

[tool.loom]
modules = []
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(FLOWS_PYPROJECT)
    return tmp_path


def loom(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NO_COLOR": "1"}
    # No key: the point is the shape of the output, not a real generation.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


class TestTheCommandStillBehaves:
    """Progress must not cost the machine-readable contract."""

    def test_json_mode_stays_parseable_when_authoring_fails(
        self, project: Path
    ) -> None:
        """With no provider key the command reports a reason, not a spinner."""
        done = loom(project, "author", "anything", "--json")
        assert done.returncode == Exit.USAGE, done.stdout
        assert "needs a model" in done.stderr
        assert done.stdout.strip() == "" or json.loads(done.stdout)

    def test_progress_never_lands_on_stdout(self, project: Path) -> None:
        """``loom author "spec" > flow.py`` has to put the *code* in the file."""
        done = loom(project, "author", "anything")
        assert "⏺" not in done.stdout


class TestBudgetsReachTheCli:
    """`max_total_tokens` and `max_cost_usd` existed on the agent and reached
    no surface, so `--max-cost` bounded nothing anybody could set."""

    def test_the_flags_exist(self, project: Path) -> None:
        help_text = loom(project, "author", "--help").stdout
        assert "--max-tokens" in help_text
        assert "--max-cost" in help_text

    def test_they_cross_the_port(self) -> None:
        import inspect

        from loom.facade import LocalFacade, RemoteFacade, RuntimeFacade
        from loom.identity.facade import AuthorizedFacade

        for adapter in (RuntimeFacade, LocalFacade, RemoteFacade, AuthorizedFacade):
            params = inspect.signature(adapter.author).parameters
            assert "max_tokens" in params, adapter.__name__
            assert "max_cost" in params, adapter.__name__

    async def test_a_token_ceiling_stops_the_job(self) -> None:
        """And reports what it spent, so the number that says whether to raise
        it is the one that was thrown away before."""
        agent = WorkflowCodingAgent(
            scripted(tool="search_toolsets"), smoke_test=False, max_total_tokens=1
        )
        result = await agent.generate("do a thing")
        assert result.code == ""
        assert result.issues and result.issues[0].category == "unsupported"

    async def test_it_names_the_dial_that_stopped_it(self) -> None:
        """Naming the wrong one is the same defect as naming none: a job
        stopped by a token ceiling was told to raise its *turn* budget."""
        agent = WorkflowCodingAgent(
            scripted(tool="search_toolsets"), smoke_test=False, max_total_tokens=1
        )
        message = (await agent.generate("do a thing")).issues[0].message
        assert "--max-tokens" in message
        assert "max_discovery_turns" not in message

    async def test_a_turn_limit_still_names_turns(self) -> None:
        agent = WorkflowCodingAgent(
            scripted(tool="search_toolsets"),
            smoke_test=False,
            max_discovery_turns=1,
            max_repair_attempts=0,
        )
        message = (await agent.generate("do a thing")).issues[0].message
        assert "max_discovery_turns" in message


class TestTurnBudget:
    """The job budget is discovery + repair, and the message names the flag."""

    def test_the_default_job_budget_is_thirty(self) -> None:
        import inspect

        params = inspect.signature(WorkflowCodingAgent.__init__).parameters
        discovery = params["max_discovery_turns"].default
        repair = params["max_repair_attempts"].default
        assert discovery + repair == 30

    def test_the_help_states_the_real_number(self) -> None:
        """A default in the help that disagrees with the code sends people to
        raise a limit they had already cleared."""
        import inspect

        from loom.cli import build_parser

        params = inspect.signature(WorkflowCodingAgent.__init__).parameters
        discovery = params["max_discovery_turns"].default
        for action in build_parser()._subparsers._group_actions:
            author = (action.choices or {}).get("author")
            if author is None:
                continue
            turns = next(a for a in author._actions if "--turns" in a.option_strings)
            assert f"default {discovery}" in (turns.help or "")

    async def test_running_out_of_turns_names_the_flag(self) -> None:
        """Not `max_turns`, which is a constructor argument nobody running
        `loom author` is holding."""
        looping = MockModelProvider(
            responses=[
                mock_response(
                    tool_calls=[
                        ToolCall(
                            id=str(n), name="search_toolsets", arguments={"query": "x"}
                        )
                    ]
                )
                for n in range(40)
            ]
        )
        agent = WorkflowCodingAgent(
            looping, smoke_test=False, max_discovery_turns=3, max_repair_attempts=1
        )
        message = (await agent.generate("x")).issues[0].message
        assert "--turns" in message
        assert "max_turns" not in message
        # And exactly one remedy, not the library's plus ours.
        assert message.count("narrow the") == 1

    def test_the_fact_survives_a_reworded_library_message(self) -> None:
        """Cutting is narrow on purpose: degrading to redundancy is better
        than degrading to a lost explanation."""
        from loom.agents.coding_agent import _without_generic_advice

        assert (
            _without_generic_advice("ran out of turns; raise max_turns or narrow")
            == "ran out of turns"
        )
        assert _without_generic_advice("something else entirely") == (
            "something else entirely"
        )


class TestNothingElsePaintsWhileAQuestionIsUp:
    """Two writers, one terminal.

    `ProgressRenderer` keeps a `rich` live region on stderr and redraws it on
    its own timer from the main thread. `PromptUserInteraction` runs a
    full-screen `prompt_toolkit` dialog on the same stderr from a worker
    thread. Both painted at once, and the dialog came up with scrollback
    bleeding through it and the question rendered twice — once in the box and
    once in the line showing through it.

    Neither component's own tests can see that: each is correct alone, and the
    fault is in the pair. Same shape as `tests/test_ask_user_wiring.py`, which
    exists because `interaction.py` shipped complete and wired by nobody.
    """

    def test_a_drawing_renderer_registers_itself(self) -> None:
        from loom.cli import progress

        renderer = progress.ProgressRenderer(enabled=True, live_capable=True)
        renderer._console = _FakeConsole()
        renderer._begin("thinking")
        try:
            assert any(active is renderer for active in progress._ACTIVE)
        finally:
            renderer.close()
        assert not any(active is renderer for active in progress._ACTIVE)

    def test_suspend_stops_it_and_starts_it_again(self) -> None:
        from loom.cli import progress

        renderer = progress.ProgressRenderer(enabled=True, live_capable=True)
        renderer._console = _FakeConsole()
        renderer._begin("thinking")
        live = renderer._live
        try:
            with progress.suspend():
                assert renderer._live is None, "the live region kept drawing"
                assert not live.is_started, "rich.Live was detached but not stopped"
            assert renderer._live is not None, "the live region did not come back"
        finally:
            renderer.close()

    def test_suspend_is_a_no_op_when_nothing_is_drawing(self) -> None:
        from loom.cli import progress

        with progress.suspend():
            pass

    def test_the_interaction_actually_calls_it(self) -> None:
        """The wiring. A `suspend()` nobody calls is the bug still shipping."""
        import inspect

        from loom.cli.repl.interaction import PromptUserInteraction

        source = inspect.getsource(PromptUserInteraction.ask)
        assert "suspend()" in source

    def test_the_dialog_carries_the_question_rather_than_printing_it(self) -> None:
        """It covers the screen, so anything printed beside it shows through."""
        import inspect

        from loom.cli.repl.interaction import PromptUserInteraction

        source = inspect.getsource(PromptUserInteraction._one)
        # The headline is for the path that renders nothing of its own.
        assert source.index("self._headline") > source.index("question.kind ==")


class _FakeConsole:
    """Enough of a `rich` console for `_begin` to build a Live against."""

    is_terminal = True

    def __getattr__(self, name: str) -> object:
        return lambda *a, **k: None


class TestConnectingHoldsTheTerminalToo:
    """The same two-writers-one-terminal fault, one subsystem over.

    `connect_toolset` printed "atlassian client id:" and the progress spinner
    kept redrawing on the same stderr on its own timer, landing between the two
    prompts:

        atlassian client id:
        ◐ connect_toolset("jira")   turn 5 · 76.9k tok · $0.16 · 1m39s
        atlassian client secret:

    Fixed for `ask_user` and not applied here, because each was wired
    separately — which is why this asserts the *wiring* rather than `suspend()`
    itself, already covered above.
    """

    async def test_the_secret_prompt_holds_the_live_region(self) -> None:
        import loom.connectors.flows as flows
        from loom.cli import progress, targets
        from loom.toolsets.manifest import AuthField

        renderer = progress.ProgressRenderer(enabled=True, live_capable=True)
        renderer._console = _FakeConsole()
        renderer._begin("thinking")

        during: list[object] = []
        original = flows.ConsoleSecretPrompt.read

        async def spy(self: object, field: AuthField) -> str:
            during.append(renderer._live)
            return "typed"

        flows.ConsoleSecretPrompt.read = spy  # type: ignore[method-assign]
        try:
            assert await targets._secret_prompt().read(AuthField(name="CLIENT_ID"))
        finally:
            flows.ConsoleSecretPrompt.read = original  # type: ignore[method-assign]
            renderer.close()

        assert during == [None], "the spinner kept drawing over the prompt"

    def test_the_event_renderer_holds_it_as_well(self) -> None:
        """The printed block is overwritten just as readily as a prompt."""
        import inspect

        from loom.cli import targets

        assert "suspend()" in inspect.getsource(targets._connect_renderer)

    def test_the_redirect_uri_is_the_one_the_listener_will_bind(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """It was a hardcoded 8931, so anyone who had set the port was told to
        register a URI the flow was never going to use."""
        from loom.cli import targets
        from loom.connectors.flows import ConnectEvent

        monkeypatch.setenv("LOOM_OAUTH_REDIRECT_PORT", "9002")
        targets._connect_renderer()(ConnectEvent(kind="needs_app", detail="x"))

        assert "http://127.0.0.1:9002/callback" in capsys.readouterr().err

    def test_the_heading_is_not_said_twice(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """It read: "No OAuth app is registered yet — no OAuth app is
        registered for 'atlassian' yet"."""
        from loom.cli import targets
        from loom.connectors.flows import ConnectEvent

        targets._connect_renderer()(
            ConnectEvent(kind="needs_app", detail="No OAuth app for 'atlassian' yet.")
        )
        err = capsys.readouterr().err
        assert err.lower().count("no oauth app") == 1
