"""Every Python example in the documentation must at least compile.

A README snippet that cannot run is worse than no snippet: it is the first
thing a new reader tries, and it fails before they have learned anything about
the project. Three shipped that way — top-level ``await``, which is valid inside
a function and a ``SyntaxError`` in a script.

``compile`` rather than ``ast.parse``. The parser accepts top-level ``await``;
only compilation rejects it, which is the same distinction
:func:`workflow_builder.agents.smoke.compile_check` exists for — and the reason
a first attempt at this test passed while the README was broken.

Deliberately not checked: whether an example survives being pasted into the
3.12 REPL, where a blank line closes the block it sits in. Enforcing that would
ban blank lines between methods and inside docstrings — ordinary, correct Python
— to suit one way of running the code. The Quick Start avoids them because it is
the block people paste; the rest say to save the file and run it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"```python\n(.*?)```", re.S)

#: A page may declare imports its examples share, so a guide with ten short
#: blocks does not repeat four import lines ten times. The preamble is checked
#: as a block itself and prepended to every later block on that page.
PREAMBLE = "<!-- docs-preamble -->"


def preamble_of(text: str) -> str:
    """The shared imports a page declares, or ``""``."""
    marker = text.find(PREAMBLE)
    if marker == -1:
        return ""
    match = BLOCK.search(text, marker)
    return match.group(1) if match else ""


def markdown_files() -> list[Path]:
    """The docs a reader is most likely to copy from."""
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    return [path for path in files if path.exists()]


def blocks(path: Path) -> list[tuple[int, str]]:
    """Every Python block in *path*, with the line it starts on.

    Each carries its page's preamble, so what is checked is what a reader ends
    up with after copying the shared imports and the block they want.
    """
    text = path.read_text(encoding="utf-8")
    shared = preamble_of(text)
    return [
        (
            text[: match.start()].count("\n") + 1,
            match.group(1)
            if match.group(1) == shared
            else f"{shared}\n{match.group(1)}",
        )
        for match in BLOCK.finditer(text)
    ]


CASES = [
    pytest.param(path, line, code, id=f"{path.name}:{line}")
    for path in markdown_files()
    for line, code in blocks(path)
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
