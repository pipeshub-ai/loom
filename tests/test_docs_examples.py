"""Every Python example in the documentation must at least compile.

A README snippet that cannot run is worse than no snippet: it is the first
thing a new reader tries, and it fails before they have learned anything about
the project. Three shipped that way — top-level ``await``, which is valid inside
a function and a ``SyntaxError`` in a script.

``compile`` rather than ``ast.parse``. The parser accepts top-level ``await``;
only compilation rejects it, which is the same distinction
:func:`loom.agents.smoke.compile_check` exists for — and the reason
a first attempt at this test passed while the README was broken.

Deliberately not checked: whether an example survives being pasted into the
3.12 REPL, where a blank line closes the block it sits in. Enforcing that would
ban blank lines between methods and inside docstrings — ordinary, correct Python
— to suit one way of running the code. The Quick Start avoids them because it is
the block people paste; the rest say to save the file and run it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from docs_examples import examples  # noqa: E402 - needs the path above

CASES = [
    pytest.param(e.path, e.line, e.code, id=f"{e.path.name}:{e.line}")
    for e in examples()
]


@pytest.mark.parametrize(("path", "line", "code"), CASES)
def test_every_documented_example_compiles(path: Path, line: int, code: str) -> None:
    try:
        compile(code, f"{path.name}:{line}", "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"{path.relative_to(ROOT)} line {line}: {exc.msg} "
            f"(block line {exc.lineno})\n\n{code}"
        )


@pytest.mark.parametrize(("path", "line", "code"), CASES)
def test_every_documented_example_resolves_its_names(
    path: Path, line: int, code: str
) -> None:
    """No name used without being imported or defined.

    Compiling is not enough: a block referencing ``Runtime`` without importing
    it compiles perfectly and fails the moment anyone runs it. This is the check
    that catches a fragment presented as an example.
    """
    import json
    import subprocess
    import sys
    import tempfile

    ruff = Path(sys.executable).parent / "ruff"
    if not ruff.exists():
        pytest.skip("ruff is not installed")

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "block.py"
        target.write_text(code, encoding="utf-8")
        completed = subprocess.run(
            [str(ruff), "check", "--select", "F821", "--output-format", "json",
             "--no-cache", str(target)],
            capture_output=True,
            text=True,
        )

    findings = json.loads(completed.stdout or "[]")
    names = sorted(
        {f["message"].split("`")[1] for f in findings if "`" in f["message"]}
    )
    assert not names, (
        f"{path.relative_to(ROOT)} line {line}: uses {names} without importing "
        "or defining them. Add the imports, or put them in the page preamble."
    )


def test_there_are_examples_to_check() -> None:
    """A regex that silently matches nothing would make this suite vacuous."""
    assert len(CASES) >= 6


class TestExampleRunner:
    """The script the CI job runs.

    Its judgement call is which failures count. A missing database is about the
    machine; a wrong keyword argument is about the documentation. Getting that
    backwards gives either a red build nobody trusts or a green one that means
    nothing.
    """

    def test_a_missing_dependency_is_environmental(self) -> None:
        from docs_examples import is_environmental

        assert is_environmental("ModuleNotFoundError: No module named 'motor'")
        assert is_environmental("ModuleNotFoundError: No module named 'asyncpg'")
        assert is_environmental("ModuleNotFoundError: No module named 'langchain_anthropic'")

    def test_a_missing_credential_is_environmental(self) -> None:
        from docs_examples import is_environmental

        assert is_environmental("anthropic.AuthenticationError: API key is invalid")
        assert is_environmental("KeyError: 'ANTHROPIC_API_KEY'")

    def test_an_unreachable_service_is_environmental(self) -> None:
        from docs_examples import is_environmental

        assert is_environmental("ServerSelectionTimeoutError: localhost:27017")
        assert is_environmental("ConnectionRefusedError: [Errno 61] Connection refused")

    def test_a_wrong_api_is_not_environmental(self) -> None:
        """The failure this whole job exists to catch."""
        from docs_examples import is_environmental

        assert not is_environmental(
            "TypeError: Retry.__init__() got an unexpected keyword argument 'backoff'"
        )
        assert not is_environmental(
            "ImportError: cannot import name 'AnthropicProvider' "
            "from 'loom.agents.models'"
        )
        assert not is_environmental("AttributeError: 'CodingResult' object has no attribute 'load'")

    def test_it_finds_the_documented_examples(self) -> None:
        from docs_examples import examples

        found = examples()
        assert len(found) >= 55
        assert any(e.path.name == "README.md" for e in found)

    def test_a_page_preamble_is_prepended(self) -> None:
        """A guide declares shared imports once; every block is checked with them."""
        from docs_examples import examples

        triggers = [e for e in examples() if e.path.name == "triggers.md"]
        assert triggers
        assert all("from loom import" in e.code for e in triggers)

    def test_running_a_good_example_reports_ok(self, tmp_path: Path) -> None:
        from docs_examples import Example, run

        example = Example(tmp_path / "x.md", 1, "print('hello')\n")
        assert run(example, timeout=30)[0] == "ok"

    def test_running_a_broken_example_reports_failed(self, tmp_path: Path) -> None:
        from docs_examples import Example, run

        example = Example(tmp_path / "x.md", 1, "raise TypeError('bad keyword')\n")
        outcome, detail = run(example, timeout=30)
        assert outcome == "failed"
        assert "bad keyword" in detail

    def test_a_missing_import_is_skipped_not_failed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An example needing a dependency this machine lacks is not wrong.

        The absent module is injected rather than named from the real list: the
        list holds *optional extras*, and the store-parity job installs
        ``[mongo,postgres]`` on purpose — so an assertion built on ``motor``
        being missing passes until somebody installs the thing CI needs.
        """
        import docs_examples
        from docs_examples import Example, run

        monkeypatch.setattr(
            docs_examples,
            "ENVIRONMENTAL",
            (*docs_examples.ENVIRONMENTAL, "no module named 'definitely_absent_pkg'"),
        )
        example = Example(tmp_path / "x.md", 1, "import definitely_absent_pkg\n")
        assert run(example, timeout=30)[0] == "skipped"

    def test_a_hanging_example_is_a_failure(self, tmp_path: Path) -> None:
        """An example that never returns is a defect, not a slow machine."""
        from docs_examples import Example, run

        example = Example(tmp_path / "x.md", 1, "import time; time.sleep(30)\n")
        outcome, detail = run(example, timeout=2)
        assert outcome == "failed"
        assert "did not finish" in detail


class TestReadmeExamplesDemonstrate:
    """A README block that runs silently teaches nothing.

    Three shipped that way: they built a Runtime, defined a workflow, and
    exited — so the reader saw no output and reasonably concluded it was
    broken. The check is deliberately limited to the README. A guide showing
    how a trigger is *declared* has nothing to print, and demanding output
    there would make reference material worse.
    """

    def test_every_readme_block_prints_something(self) -> None:
        import subprocess
        import sys
        import tempfile

        from docs_examples import examples, is_environmental

        silent = []
        for example in examples([ROOT / "README.md"]):
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "example.py"
                script.write_text(example.code, encoding="utf-8")
                try:
                    done = subprocess.run(
                        [sys.executable, str(script)],
                        capture_output=True, text=True, timeout=240, cwd=tmp,
                        stdin=subprocess.DEVNULL,
                    )
                except subprocess.TimeoutExpired:
                    continue
            if done.returncode != 0:
                # Needs a key or an optional extra here; the CI job covers it.
                if is_environmental(done.stderr):
                    continue
                pytest.fail(f"{example.label} failed: {done.stderr.strip()[-200:]}")
            if not done.stdout.strip():
                silent.append(example.label)

        assert not silent, (
            f"README blocks that run but print nothing: {silent}. "
            "A reader runs these first; one that outputs nothing reads as broken."
        )
