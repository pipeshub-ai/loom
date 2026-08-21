"""The interactive session, and the thing it must never become.

A session is a second surface, and a second surface is where a CLI's guarantees
go to drift. So the load-bearing test here is not "does ``/run`` run something" —
it is that **``/run`` cannot be anything other than ``loom run``**, because it
is parsed by the same parser and dispatched to the same handler. A registry of
small functions calling the facade would pass every behavioural test in this
file and be wrong in six months.

The rest covers the two things the session genuinely owns: which file free text
acts on, and refusing to end the conversation over one bad command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from loom.cli import _HANDLERS, build_parser
from loom.cli.config import ProjectConfig
from loom.cli.output import Exit, Printer
from loom.cli.repl import available
from loom.cli.repl.commands import SLASH, known, resolve_alias
from loom.cli.repl.session import Session

FLOWS = '''
from __future__ import annotations

from loom import Context, step, workflow


@step
async def echo(text: str) -> str:
    return text


@workflow(name="sessioned")
async def sessioned(ctx: Context, text: str = "hi") -> str:
    """Something to run from the session."""
    return await ctx.step(echo, text)
'''

PYPROJECT = """
[project]
name = "sessiontest"
version = "0.1.0"

[tool.loom]
modules = ["flows.py"]
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "flows.py").write_text(FLOWS)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOM_STORE", raising=False)
    return tmp_path


@pytest.fixture
def session(project: Path) -> Session:
    config = ProjectConfig.discover(project)
    config.prepare()
    return Session(project=config, out=Printer())


class TestSlashCommandsAreTheSubcommands:
    """The invariant. Everything else in this file is behaviour on top of it."""

    def test_every_slash_command_names_a_real_subcommand(self) -> None:
        """A name here that argparse does not know is a command that 404s."""
        parser = build_parser()
        subcommands = {
            choice
            for action in parser._subparsers._group_actions  # type: ignore[union-attr]
            for choice in (action.choices or {})
        }
        for command in known():
            if command.subcommand:
                assert command.subcommand in subcommands, command.name

    def test_every_slash_command_reaches_a_handler(self) -> None:
        for command in known():
            if command.subcommand:
                assert command.subcommand in _HANDLERS, command.name

    def test_the_preset_arguments_parse(self) -> None:
        """``/failed`` is ``runs --status failed``; if that stops parsing, say so."""
        parser = build_parser()
        for command in known():
            if command.subcommand and command.preset:
                parsed = parser.parse_args([command.subcommand, *command.preset])
                assert parsed.command == command.subcommand

    def test_session_only_commands_own_no_subcommand(self) -> None:
        """The split is the rule: anything touching a Runtime is a subcommand,
        so it is reachable from a script too. Only ``/exit``-shaped things are
        the session's."""
        owned = {c.name for c in known() if not c.subcommand}
        assert owned == {"help", "exit", "quit", "clear", "open", "new", "status"}

    def test_there_is_no_second_implementation(self) -> None:
        """``commands.py`` must not import the facade.

        The moment it does, a slash command can do something ``loom`` cannot,
        and that is exactly the drift this arrangement exists to make
        impossible.
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "loom"
            / "cli"
            / "repl"
            / "commands.py"
        ).read_text()
        assert "RuntimeFacade" not in source
        assert "loom.facade" not in source


class TestAliases:
    def test_exact_names_win(self) -> None:
        """``run`` is a prefix of ``runs``, and must still mean ``run``."""
        assert resolve_alias("run") is SLASH["run"]
        assert resolve_alias("runs") is SLASH["runs"]

    def test_unique_prefixes_resolve(self) -> None:
        assert resolve_alias("work") is SLASH["workflows"]
        assert resolve_alias("pen") is SLASH["pending"]

    def test_ambiguous_prefixes_refuse(self) -> None:
        """Guessing is how a session cancels a run when asked to check one."""
        assert resolve_alias("c") is None  # cancel / check / connect
        assert resolve_alias("ru") is None  # run / runs
        assert resolve_alias("nod") is None  # node / nodes

    def test_two_names_for_one_thing_are_not_ambiguous(self) -> None:
        assert resolve_alias("nonsense") is None

    def test_an_abbreviation_is_not_a_prefix(self) -> None:
        """Accepting ``/wf`` would mean deciding what counts as an abbreviation."""
        assert resolve_alias("wf") is None


class TestFocus:
    """Which file free text acts on."""

    def test_a_new_session_has_none(self, session: Session) -> None:
        assert session.focus is None

    def test_open_sets_it(self, session: Session, project: Path) -> None:
        assert session.handle("/open flows.py") == Exit.OK
        assert session.focus == project / "flows.py"

    def test_open_refuses_a_missing_file(self, session: Session) -> None:
        assert session.handle("/open nope.py") == Exit.USAGE
        assert session.focus is None

    def test_new_clears_it(self, session: Session) -> None:
        session.handle("/open flows.py")
        session.handle("/new")
        assert session.focus is None

    def test_open_accepts_the_completion_sigil(
        self, session: Session, project: Path
    ) -> None:
        """``@flows.py`` is what Tab completion inserts, so it has to work."""
        session.handle("/open @flows.py")
        assert session.focus == project / "flows.py"


class TestDerivedFilenames:
    """A session that produces ``workflow_3.py`` is one whose output nobody
    can find afterwards."""

    def test_it_reads_like_the_description(self, session: Session) -> None:
        path = session._next_filename("watch a folder and summarise new PDFs")
        assert path.name == "watch_folder_summarise.py"

    def test_junk_still_produces_a_name(self, session: Session) -> None:
        assert session._next_filename("!!! ???").name == "workflow.py"

    def test_a_collision_takes_a_suffix(self, session: Session) -> None:
        """Nothing has been shown a diff yet, so nothing may be overwritten."""
        first = session._next_filename("send a daily digest")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("# taken\n")
        second = session._next_filename("send a daily digest")
        assert second != first
        assert first.read_text() == "# taken\n"


class TestOneBadCommandDoesNotEndTheSession:
    def test_an_unknown_command_is_a_usage_error(self, session: Session) -> None:
        assert session.handle("/nonsense") == Exit.USAGE
        assert session._closing is False

    def test_a_bad_argument_does_not_exit_the_process(
        self, session: Session
    ) -> None:
        """argparse exits the process on a bad flag. In a session that would
        end the conversation over a typo."""
        assert session.handle("/runs --status not-a-status") == Exit.USAGE
        assert session._closing is False

    def test_blank_lines_are_free(self, session: Session) -> None:
        assert session.handle("   ") == Exit.OK

    def test_exit_closes(self, session: Session) -> None:
        session.handle("/exit")
        assert session._closing is True


class TestCommandsActuallyRun:
    """A thin end-to-end pass, because the invariant above says nothing about
    whether the wiring is plugged in."""

    def test_run_reports_the_run(self, session: Session, capsys) -> None:
        assert session.handle("/run sessioned -i hello") == Exit.OK
        assert "hello" in capsys.readouterr().out

    def test_the_exit_code_is_the_subcommands(self, session: Session) -> None:
        """``/run`` on a name that does not exist is a usage error, not a
        session error, and the value is the one a script would see."""
        assert session.handle("/run no_such_workflow") == Exit.USAGE

    def test_runs_sees_what_run_recorded(self, session: Session, capsys) -> None:
        session.handle("/run sessioned -i kept")
        capsys.readouterr()
        session.handle("/runs")
        assert "sessioned" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def loom(cwd: Path, *args: str, stdin: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "LOOM_STORE"}
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=120,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
    )


class TestBareLoom:
    """``loom`` on its own. Safe for everything already written against it."""

    def test_piped_still_prints_help(self, project: Path) -> None:
        """A script, a CI job, or a pipe gets exactly what it always got."""
        done = loom(project)
        assert done.returncode == Exit.OK
        assert "usage: loom" in done.stdout
        assert "author" in done.stdout

    def test_json_asks_for_help_not_a_prompt(self, project: Path) -> None:
        """Asking for machine output and being handed a prompt is the worst of
        both."""
        done = loom(project, "--json")
        assert done.returncode == Exit.OK
        assert "usage: loom" in done.stdout

    def test_the_session_is_reachable_in_this_install(self) -> None:
        assert available() is True

    def test_subcommands_are_untouched(self, project: Path) -> None:
        done = loom(project, "run", "sessioned", "-i", "still-here", "--json")
        assert done.returncode == Exit.OK, done.stderr
        assert json.loads(done.stdout)["output"] == "still-here"


class TestPickerAddsRenderingNotRules:
    """The fourth ``UserInteraction`` must not be a fourth set of rules."""

    def test_it_delegates_the_rules(self) -> None:
        from loom.agents.interaction import CLIUserInteraction
        from loom.cli.repl.interaction import PromptUserInteraction

        picker = PromptUserInteraction()
        assert isinstance(picker._plain, CLIUserInteraction)

    def test_a_non_tty_is_unavailable(self) -> None:
        """So the tool is never offered where nothing can answer it."""
        from loom.cli.repl.interaction import PromptUserInteraction

        # Under pytest stdin is not a terminal, which is the case that matters.
        assert PromptUserInteraction().available() is False

    async def test_unavailable_falls_back_rather_than_blocking(self) -> None:
        from loom.agents.interaction import Question
        from loom.cli.repl.interaction import PromptUserInteraction

        answers = await PromptUserInteraction().ask(
            [Question(question="which?", choices=[])]
        )
        assert [a.action for a in answers] == ["cancel"]
