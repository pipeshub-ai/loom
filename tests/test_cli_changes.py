"""Nothing is written before it has been shown.

``loom edit`` wrote the file and *then* printed the diff, so the first answer to
"what did that instruction do to my workflow?" arrived after the answer was the
only copy. ``loom author -o existing.py`` clobbered silently for the same
reason: the write was the first thing it did with the path.

The ordering is the fix, and it is what most of this file asserts — by checking
the *bytes on disk*, since a test that only reads stdout cannot tell "shown then
written" from "written then shown".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from loom.cli.changes import (
    Allowlist,
    Decision,
    FileChange,
    apply,
    ask,
    forget_allowlist,
    propose,
    render,
    session_allowlist,
)
from loom.cli.output import Exit, Printer

BEFORE = 'def flow():\n    return "original"\n'
AFTER = 'def flow():\n    return "edited"\n'


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "flow.py"
    path.write_text(BEFORE)
    return path


@pytest.fixture(autouse=True)
def clean_allowlist():
    forget_allowlist()
    yield
    forget_allowlist()


class TestTheChangeItself:
    def test_it_knows_it_is_creating(self, tmp_path: Path) -> None:
        assert FileChange(path=tmp_path / "new.py", after=AFTER).creating is True

    def test_it_knows_it_is_replacing(self, target: Path) -> None:
        assert FileChange(path=target, after=AFTER).creating is False

    def test_it_computes_a_diff_when_the_caller_has_none(
        self, target: Path
    ) -> None:
        """So a surface with only "here is the new content" still cannot write
        without showing what it replaces."""
        diff = FileChange(path=target, after=AFTER).unified()
        assert '-    return "original"' in diff
        assert '+    return "edited"' in diff

    def test_the_callers_own_diff_wins(self, target: Path) -> None:
        """An edit's diff is computed against the source the *model* was given.

        Recomputing from disk would describe a file changed in another window
        since as part of this change.
        """
        change = FileChange(path=target, after=AFTER, diff="--- theirs\n+++ mine\n")
        assert change.unified() == "--- theirs\n+++ mine\n"

    def test_no_change_has_no_diff(self, target: Path) -> None:
        assert FileChange(path=target, after=BEFORE).unified() == ""

    def test_the_summary_counts_both_directions(self, target: Path) -> None:
        assert FileChange(path=target, after=AFTER).summary() == "+1 -1"

    def test_a_new_file_is_all_additions(self, tmp_path: Path) -> None:
        change = FileChange(path=tmp_path / "new.py", after=AFTER)
        assert change.summary() == "+2 -0"


class TestNothingIsWrittenByShowing:
    """`render` and `propose` must be pure with respect to the filesystem."""

    def test_render_writes_nothing(self, target: Path, capsys) -> None:
        render(FileChange(path=target, after=AFTER), Printer())
        assert target.read_text() == BEFORE
        assert "edited" in capsys.readouterr().out

    def test_a_refusal_writes_nothing(self, target: Path) -> None:
        decision = propose(
            FileChange(path=target, after=AFTER), Printer(), interactive=False
        )
        assert decision is Decision.REFUSE
        assert target.read_text() == BEFORE

    def test_applying_is_a_separate_call(self, target: Path) -> None:
        change = FileChange(path=target, after=AFTER)
        propose(change, Printer(), assume_yes=True)
        assert target.read_text() == BEFORE, "propose must not write"
        apply(change, Printer())
        assert target.read_text() == AFTER


class TestNonInteractiveDenies:
    """A gate that could not run has not passed."""

    def test_it_refuses_and_names_the_flag(self, target: Path, capsys) -> None:
        propose(FileChange(path=target, after=AFTER), Printer(), interactive=False)
        assert "--yes" in capsys.readouterr().err

    def test_yes_overrides(self, target: Path) -> None:
        decision = propose(
            FileChange(path=target, after=AFTER),
            Printer(),
            assume_yes=True,
            interactive=False,
        )
        assert decision is Decision.APPLY

    def test_yes_does_not_even_render(self, target: Path, capsys) -> None:
        """A `--yes` write is a decision already taken; printing the diff over
        it is noise in a CI log."""
        propose(FileChange(path=target, after=AFTER), Printer(), assume_yes=True)
        assert capsys.readouterr().out == ""


class TestTheLadder:
    def _answer(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        monkeypatch.setattr("builtins.input", lambda *_: text)

    def test_yes(self, target: Path, monkeypatch) -> None:
        self._answer(monkeypatch, "1")
        assert ask(FileChange(path=target, after=AFTER), Printer()) is Decision.APPLY

    def test_enter_is_yes(self, target: Path, monkeypatch) -> None:
        """The common answer, one keystroke."""
        self._answer(monkeypatch, "")
        assert ask(FileChange(path=target, after=AFTER), Printer()) is Decision.APPLY

    def test_no(self, target: Path, monkeypatch) -> None:
        self._answer(monkeypatch, "3")
        assert ask(FileChange(path=target, after=AFTER), Printer()) is Decision.REFUSE

    def test_anything_unrecognised_is_no(self, target: Path, monkeypatch) -> None:
        """The safe reading of an answer nobody understood."""
        self._answer(monkeypatch, "wat")
        assert ask(FileChange(path=target, after=AFTER), Printer()) is Decision.REFUSE

    def test_interrupting_the_question_is_not_consent(
        self, target: Path, monkeypatch
    ) -> None:
        def interrupt(*_):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", interrupt)
        assert ask(FileChange(path=target, after=AFTER), Printer()) is Decision.REFUSE

    def test_a_closed_stdin_is_not_consent(self, target: Path, monkeypatch) -> None:
        def closed(*_):
            raise EOFError

        monkeypatch.setattr("builtins.input", closed)
        assert ask(FileChange(path=target, after=AFTER), Printer()) is Decision.REFUSE


class TestAlwaysRemembers:
    """A prompt on every write is one people learn to answer without reading."""

    def test_it_stops_asking_for_that_path(self, target: Path, monkeypatch) -> None:
        allowlist = Allowlist()
        monkeypatch.setattr("builtins.input", lambda *_: "2")
        first = propose(
            FileChange(path=target, after=AFTER),
            Printer(),
            allowlist=allowlist,
            interactive=True,
        )
        assert first is Decision.APPLY_ALWAYS

        # Second time: no input is read at all, so a stubbed-out `input` that
        # raises proves the question was not asked.
        def never(*_):
            raise AssertionError("asked again")

        monkeypatch.setattr("builtins.input", never)
        assert (
            propose(
                FileChange(path=target, after=AFTER),
                Printer(),
                allowlist=allowlist,
                interactive=True,
            )
            is Decision.APPLY
        )

    def test_it_is_per_path(self, tmp_path: Path) -> None:
        allowlist = Allowlist()
        allowlist.remember(tmp_path / "one.py")
        assert allowlist.allows(tmp_path / "one.py")
        assert not allowlist.allows(tmp_path / "two.py")

    def test_it_survives_a_relative_path(self, tmp_path: Path, monkeypatch) -> None:
        """`/edit flow.py` and `/edit ./flow.py` are the same file."""
        monkeypatch.chdir(tmp_path)
        allowlist = Allowlist()
        allowlist.remember(Path("flow.py"))
        assert allowlist.allows(tmp_path / "flow.py")

    def test_the_session_allowlist_is_one_object(self) -> None:
        assert session_allowlist() is session_allowlist()

    def test_it_is_never_written_to_disk(self, tmp_path: Path) -> None:
        """Persisting it would silently disarm the gate for a later, possibly
        unattended, run."""
        session_allowlist().remember(tmp_path / "flow.py")
        assert not list(tmp_path.glob("**/*allow*"))
        assert not list(tmp_path.glob("**/.loom/*"))


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

FLOW = """\
from loom import Context, workflow


@workflow(name="gated")
async def gated(ctx: Context, _: str = "") -> str:
    \"\"\"Original.\"\"\"
    return "original"
"""

PYPROJECT = """
[project]
name = "changetest"
version = "0.1.0"

[tool.loom]
modules = []
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "flow.py").write_text(FLOW)
    return tmp_path


def loom(cwd: Path, *args: str):
    env = {**os.environ, "NO_COLOR": "1"}
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


class TestTheFlagsExist:
    """Cheap, and the failure they prevent is a `--yes` that silently does
    nothing because nothing declared it."""

    def test_edit_takes_yes_and_dry_run(self, project: Path) -> None:
        help_text = loom(project, "edit", "--help").stdout
        assert "--yes" in help_text
        assert "--dry-run" in help_text

    def test_author_takes_yes(self, project: Path) -> None:
        assert "--yes" in loom(project, "author", "--help").stdout

    def test_edit_without_a_model_is_still_a_usage_error(
        self, project: Path
    ) -> None:
        """The gate must not have moved where the "no model" failure lands."""
        done = loom(project, "edit", "flow.py", "change it")
        assert done.returncode == Exit.USAGE
        assert project.joinpath("flow.py").read_text() == FLOW


class TestInterruptAdviceIsTrue:
    """Advice that cannot help is worse than none.

    ``author`` and ``edit`` start no run, and their interrupt pointed at
    ``loom runs --status running`` — sending someone to look for a run that
    never existed.
    """

    def test_a_command_that_drives_runs_names_the_recovery(self, capsys) -> None:
        from loom.cli.commands import interrupted

        interrupted(130)
        assert "loom runs --status running" in capsys.readouterr().err

    def test_a_command_that_starts_none_says_so(self, capsys) -> None:
        from loom.cli.commands import interrupted

        interrupted(130, drives_runs=False)
        err = capsys.readouterr().err
        assert "loom runs --status running" not in err
        assert "Nothing to clean up" in err

    def test_authoring_opts_out(self) -> None:
        """Asserted on the source, because the alternative is interrupting a
        real authoring run in a test to read one line of stderr."""
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "loom"
            / "cli"
            / "commands.py"
        ).read_text()
        for command in ("cmd_author", "cmd_edit"):
            start = source.index(f"def {command}(")
            end = source.index("\ndef ", start + 1)
            assert "drives_runs=False" in source[start:end], command
