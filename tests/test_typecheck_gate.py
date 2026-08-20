"""The type-check gate, driven against mypy outputs it must tell apart.

``scripts/typecheck.py`` exists because mypy has a failure mode that reads like
an ordinary one: under an ``[all]`` environment it stops on a numpy stub with
*"errors prevented further checking"* — a handful of errors, none of them
yours, exit code 1, and **nothing type-checked**. That is how 42 real errors
across three toolsets went unnoticed.

So the gate's whole job is a three-way distinction, and these tests are that
distinction. Nothing here runs mypy: the subprocess is replaced, because what
is under test is the reading of the summary line, not mypy itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import typecheck

CLEAN = "Success: no issues found in 397 source files\n"
WITH_ERRORS = (
    "src/loom/x.py:1: error: Incompatible return value type  [return-value]\n"
    "Found 4 errors in 3 files (checked 397 source files)\n"
)
GAVE_UP = (
    ".venv/lib/python3.12/site-packages/numpy/__init__.pyi:737: error: "
    "Type statement is only supported in Python 3.12 and greater  [syntax]\n"
    "Found 1 error in 1 file (errors prevented further checking)\n"
)
NARROWED = "Success: no issues found in 3 source files\n"


@pytest.fixture()
def mypy_says(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the mypy subprocess with a canned stdout and exit code."""

    def arrange(output: str, returncode: int) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=output, stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

    return arrange


class TestTheThreeWayDistinction:
    def test_a_clean_run_passes(self, mypy_says: Any) -> None:
        mypy_says(CLEAN, 0)
        assert typecheck.run([]) == 0

    def test_real_type_errors_keep_mypys_own_exit_code(self, mypy_says: Any) -> None:
        """Exit 1, not 2.

        The wrapper is a drop-in: ordinary type errors must look exactly as
        they did, or the distinction it adds is lost in the noise it makes.
        """
        mypy_says(WITH_ERRORS, 1)
        assert typecheck.run([]) == 1

    def test_giving_up_is_its_own_exit_code(self, mypy_says: Any) -> None:
        """Exit 2 — the failure the gate exists for.

        Distinguishable from exit 1 by anything reading the code rather than
        the text, which is the point: "the gate did not run" and "the gate
        failed" are different facts.
        """
        mypy_says(GAVE_UP, 1)
        assert typecheck.run([]) == 2

    def test_giving_up_is_not_mistaken_for_success(self, mypy_says: Any) -> None:
        """Even if mypy somehow exits 0 having checked nothing."""
        mypy_says(GAVE_UP, 0)
        assert typecheck.run([]) == 2


class TestItRefusesEvidenceItDoesNotHave:
    def test_no_summary_line_is_a_failure(self, mypy_says: Any) -> None:
        """A crash, a killed process, a future mypy that words it differently.

        Absent evidence that the tree was checked, the honest answer is
        failure — the same rule ``run_examples.py`` follows for a skip.
        """
        mypy_says("mypy: something went sideways\n", 1)
        assert typecheck.run([]) == 2

    def test_an_empty_output_is_a_failure(self, mypy_says: Any) -> None:
        mypy_says("", 0)
        assert typecheck.run([]) == 2

    def test_a_suspiciously_narrow_run_is_a_failure(self, mypy_says: Any) -> None:
        """A narrowed `packages` or a stray argument checks three files and
        reports success — which is a gate in name only."""
        mypy_says(NARROWED, 0)
        assert typecheck.run([]) == 2

    def test_the_advice_names_the_environment(self, mypy_says: Any, capsys: Any) -> None:
        """The message has to say what to *do*. "errors prevented further
        checking" is mypy's words and explains nothing to a reader."""
        mypy_says(GAVE_UP, 1)
        typecheck.run([])
        printed = capsys.readouterr().err
        assert "[dev,mcp]" in printed
        assert "numpy" in printed


class TestStrictEnv:
    def test_it_refuses_when_numpy_is_importable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(typecheck, "numpy_present", lambda: True)
        assert typecheck.main(["--strict-env"]) == 2

    def test_it_runs_when_numpy_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, mypy_says: Any
    ) -> None:
        monkeypatch.setattr(typecheck, "numpy_present", lambda: False)
        mypy_says(CLEAN, 0)
        assert typecheck.main(["--strict-env"]) == 0

    def test_it_is_off_by_default(
        self, monkeypatch: pytest.MonkeyPatch, mypy_says: Any
    ) -> None:
        """CI installs `[dev]`, so the check is belt-and-braces rather than the
        mechanism — and a contributor with numpy around should still get a
        useful run rather than a refusal."""
        monkeypatch.setattr(typecheck, "numpy_present", lambda: True)
        mypy_says(CLEAN, 0)
        assert typecheck.main([]) == 0


class TestCiUsesIt:
    def test_the_typecheck_job_is_blocking_and_goes_through_the_wrapper(self) -> None:
        """`mypy || true` let 42 real errors merge green."""
        import yaml

        workflow = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml")
            .read_text(encoding="utf-8")
        )
        commands = [
            step.get("run", "") for step in workflow["jobs"]["typecheck"]["steps"]
        ]
        assert any("scripts/typecheck.py" in c for c in commands)
        assert not any("|| true" in c for c in commands), (
            "an advisory type check is one that lets type errors merge"
        )

    def test_it_installs_the_dev_extra_not_all(self) -> None:
        """`[all]` pulls numpy, which is the trap the wrapper reports.

        `[dev,mcp]` is the environment: `mcp` carries no numpy, and without it
        `mcp.*` falls to `ignore_missing_imports` and the whole MCP surface is
        checked against `Any`.
        """
        import yaml

        workflow = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml")
            .read_text(encoding="utf-8")
        )
        installs = [
            step.get("run", "")
            for step in workflow["jobs"]["typecheck"]["steps"]
            if "pip install" in step.get("run", "")
        ]
        assert installs, "the typecheck job installs nothing"
        assert all('"[all]"' not in c and '".[all]"' not in c for c in installs), installs
        assert all('".[dev' in c for c in installs), installs
        assert all('mcp' in c for c in installs), (
            "without the mcp extra, mypy checks mcp_server/ against Any", installs
        )
