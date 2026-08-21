"""What the CLI prints, and what it exits with while printing it.

These are the cases the existing suite could not have caught, because every one
of them needs *data* the renderer did not author — a workflow's own output, a
step id, a path, an exception message. Rich reads a bracket in any of those as
markup, and the failures that produced were all of the same shape: a rendering
fault changing what the command reported.

Three contracts are asserted here:

* **A rendering fault can never change an exit code.** A run that completed
  exits 0 whatever its output happens to contain.
* **The plain-text path deletes nothing.** ``pip install -e '.[dev]'`` survives.
* **``--json`` stays parseable**, which every renderer touched here can break
  by writing to stdout out of turn.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from loom.cli.output import Exit, Printer, esc, exit_for

#: Strings that a markup parser reads as instructions rather than as text. The
#: second is the one that raised: a closing tag with no opener is a hard error
#: in rich, not a silent deletion.
HOSTILE = [
    "matched [/tag] in text",
    "row [0] of [1]",
    "install with .[dev]",
    "[bold]not actually bold[/bold]",
    "unclosed [dim",
    "[/]",
]

FLOWS = '''
from __future__ import annotations

from loom import Context, step, workflow


@step
async def emit(text: str) -> str:
    return text


@workflow(name="hostile")
async def hostile(ctx: Context, text: str) -> str:
    """Return whatever it was given, brackets included."""
    return await ctx.step(emit, text)


@workflow(name="defaulted")
async def defaulted(ctx: Context, text: str = "declared-default") -> str:
    """A body with a default, which an absent --input must not override."""
    return await ctx.step(emit, text)


@workflow(name="exploder")
async def exploder(ctx: Context, _: str = "") -> str:
    """Fail with a message full of markup."""
    raise RuntimeError("no such key [id] in [/records]")
'''

PYPROJECT = """
[project]
name = "rendertest"
version = "0.1.0"

[tool.loom]
modules = ["flows.py"]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "flows.py").write_text(FLOWS)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


def loom(project: Path, *args: str, color: bool = False):
    """Invoke the CLI the way a user does.

    *color* forces rich to render as if attached to a terminal, which is the
    path that parses markup — and therefore the only path that could raise.
    """
    import os

    env = {**os.environ, "LOOM_STORE": f"sqlite://{project / 'runs.db'}"}
    if color:
        env["FORCE_COLOR"] = "1"
        env["TERM"] = "xterm-256color"
    else:
        env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        capture_output=True,
        text=True,
        cwd=project,
        env=env,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


class TestDataNeverBecomesMarkup:
    """A completed run stays a completed run whatever it returned."""

    @pytest.mark.parametrize("text", HOSTILE)
    @pytest.mark.parametrize("color", [False, True])
    def test_run_output_containing_markup_still_exits_zero(
        self, project: Path, text: str, color: bool
    ) -> None:
        done = loom(project, "run", "hostile", "-i", text, color=color)
        assert done.returncode == Exit.OK, done.stderr
        assert "Traceback" not in done.stderr
        assert "MarkupError" not in done.stderr

    @pytest.mark.parametrize("text", HOSTILE)
    def test_the_output_is_printed_intact(self, project: Path, text: str) -> None:
        """Not merely survivable — the value has to arrive whole.

        Escaping that swallowed the brackets would pass the test above while
        showing the reader something the run did not return.
        """
        done = loom(project, "run", "hostile", "-i", text)
        assert text in done.stdout, done.stdout

    def test_an_error_message_containing_markup_is_reported(
        self, project: Path
    ) -> None:
        done = loom(project, "run", "exploder", color=True)
        assert done.returncode == Exit.FAILED
        assert "Traceback" not in done.stderr
        assert "[/records]" in done.stdout or "[/records]" in done.stderr

    @pytest.mark.parametrize("text", HOSTILE)
    def test_json_mode_stays_parseable(self, project: Path, text: str) -> None:
        done = loom(project, "run", "hostile", "-i", text, "--json")
        assert done.returncode == Exit.OK, done.stderr
        assert json.loads(done.stdout)["output"] == text

    def test_show_and_runs_render_the_same_run(self, project: Path) -> None:
        """The other two renderers that carry a run's own data."""
        started = json.loads(
            loom(project, "run", "hostile", "-i", HOSTILE[0], "--json").stdout
        )
        for args in (["show", started["run_id"]], ["runs"]):
            done = loom(project, *args, color=True)
            assert done.returncode == Exit.OK, done.stderr
            assert "Traceback" not in done.stderr


class TestPrinterUnit:
    """The renderer directly, where a subprocess would only say "it worked"."""

    @pytest.mark.parametrize("text", HOSTILE)
    def test_value_never_raises(self, text: str, capsys) -> None:
        Printer().value("output", text)
        assert text in capsys.readouterr().out

    @pytest.mark.parametrize("text", HOSTILE)
    def test_status_never_raises(self, text: str) -> None:
        Printer().status({"run_id": text, "status": "completed", "workflow": text})

    @pytest.mark.parametrize("text", HOSTILE)
    def test_table_never_raises(self, text: str) -> None:
        Printer().table(["a", "b"], [[text, "completed"]], status_column=1)

    @pytest.mark.parametrize("text", HOSTILE)
    def test_error_never_raises(self, text: str) -> None:
        Printer().error(text)

    def test_line_survives_an_unescaped_call_site(self, capsys) -> None:
        """The backstop, for a site that forgets :func:`esc`.

        Not a licence to skip escaping — an escaped site keeps its styling,
        this one loses it — but a missed one must not end the command.
        """
        Printer().line("wrote [/oops]")
        assert "oops" in capsys.readouterr().out

    def test_esc_round_trips_through_the_renderer(self, capsys) -> None:
        Printer().line(f"  [dim]wrote {esc('a[/b]c')}[/dim]")
        out = capsys.readouterr().out
        assert "a[/b]c" in out
        assert "[dim]" not in out

    def test_json_mode_writes_nothing_human(self, capsys) -> None:
        """Every renderer, not only ``line``.

        ``value`` and ``table`` reach the stream directly so that data does not
        pass a markup parser, which is exactly how they can corrupt ``--json``.
        """
        printer = Printer(as_json=True)
        printer.line("human")
        printer.hint("human")
        printer.verbatim("human")
        printer.value("output", "human")
        printer.status({"run_id": "r", "status": "completed"})
        printer.table(["a"], [["human"]])
        printer.journal([{"seq": 0, "step_id": "s", "status": "completed"}])
        assert capsys.readouterr().out == ""

    def test_quiet_writes_nothing_human(self, capsys) -> None:
        printer = Printer(quiet=True)
        printer.line("human")
        printer.value("output", "human")
        printer.table(["a"], [["human"]])
        assert capsys.readouterr().out == ""


class TestStripMarkupIsNarrow:
    """The plain-text path removes our tags and nothing else."""

    def test_a_pip_extra_survives(self) -> None:
        from loom.cli.output import _strip_markup

        assert _strip_markup("pip install -e '.[dev]'") == "pip install -e '.[dev]'"

    def test_an_index_survives(self) -> None:
        from loom.cli.output import _strip_markup

        assert _strip_markup("row [0]") == "row [0]"

    def test_our_own_tags_are_removed(self) -> None:
        from loom.cli.output import _strip_markup

        assert _strip_markup("[dim]x[/dim] [red]y[/red]") == "x y"


class TestInitPrintsSomethingThatWorks:
    """The first command a new user runs.

    ``[dev]`` used to be deleted by the markup parser, so the printed install
    left them without the pytest the same line told them to run.
    """

    def test_the_extra_survives(self, tmp_path: Path) -> None:
        done = loom(tmp_path, "init", ".", color=True)
        assert done.returncode == Exit.OK, done.stderr
        assert "'.[dev]'" in done.stdout

    def test_no_pointless_cd(self, tmp_path: Path) -> None:
        assert "cd ." not in loom(tmp_path, "init", ".").stdout

    def test_a_named_directory_still_gets_its_cd(self, tmp_path: Path) -> None:
        assert "cd sub" in loom(tmp_path, "init", "sub").stdout


class TestExitCodes:
    """``exit_for`` answers two different questions and used to answer one."""

    def test_a_settled_run_maps_to_its_status(self) -> None:
        assert exit_for({"status": "completed"}) == Exit.OK
        assert exit_for({"status": "failed"}) == Exit.FAILED
        assert exit_for({"status": "suspended"}) == Exit.SUSPENDED
        assert exit_for({"status": "cancelled"}) == Exit.CANCELLED

    def test_giving_up_on_a_live_run_is_not_success(self) -> None:
        """The bug: ``running`` mapped to 0, so a timed-out watch went green."""
        assert exit_for({"status": "running"}, settled=False) == Exit.SUSPENDED
        assert exit_for({"status": "pending"}, settled=False) == Exit.SUSPENDED

    def test_reporting_a_live_run_mid_flight_is_not_a_failure(self) -> None:
        """``loom show`` on a running run is a successful report about it."""
        assert exit_for({"status": "running"}) == Exit.OK

    def test_a_settled_run_ignores_the_flag(self) -> None:
        assert exit_for({"status": "failed"}, settled=False) == Exit.FAILED


class TestDeclaredDefaults:
    """An absent ``--input`` is not the same as ``--input null``."""

    def test_the_bodys_own_default_applies(self, project: Path) -> None:
        done = loom(project, "run", "defaulted", "--json")
        assert done.returncode == Exit.OK, done.stderr
        assert json.loads(done.stdout)["output"] == "declared-default"

    def test_an_explicit_value_still_wins(self, project: Path) -> None:
        done = loom(project, "run", "defaulted", "-i", "given", "--json")
        assert json.loads(done.stdout)["output"] == "given"

    def test_explicit_null_is_still_null(self, project: Path) -> None:
        """``--input null`` says None on purpose, and must not be re-defaulted."""
        done = loom(project, "run", "defaulted", "-i", "null", "--json")
        assert json.loads(done.stdout)["output"] is None


class TestUnexpectedFailuresAreNotTracebacks:
    def test_an_unanticipated_failure_renders_as_a_line(self) -> None:
        from loom.cli.commands import unexpected

        # Direct, because manufacturing an unanticipated failure through the
        # CLI means finding a defect — which is the thing this handler exists
        # to render, not something to depend on.
        unexpected(ValueError("nope"), debug=False)
        unexpected(ValueError("nope"), debug=True)
