"""Execute every example under ``examples/``.

``scripts/docs_examples.py`` runs the code blocks in the documentation, and its
docstring gives the reason: running an example is the only check that catches a
*wrong* API. That runner reads ``README.md`` and ``docs/**/*.md`` and nothing
else, so the Python files under ``examples/`` — the cookbook, and the ten
reference workflows — were covered by no gate at all. Five of the ten stopped
importing when ``Retry`` renamed a field, and ``tests/test_phase8.py`` went on
reporting 104 passing tests over them, because every one of those tests was a
substring search against the file's text.

This runner closes that. It is deliberately a sibling of ``docs_examples.py``
rather than a flag on it: a documented block is extracted and executed in a
temporary directory, while an example on disk is executed *where it lives*,
because the cookbook imports its own ``utils`` module and the reference
workflows import each other's package.

Two kinds of file, told apart by whether one is runnable:

* A **script** — anything with an ``if __name__ == "__main__"`` block — is run
  as one, from its own directory.
* A **module** — a reference workflow, which defines steps and a workflow and
  nothing that executes — is imported. Importing is the whole check: it is what
  ``Retry(delay=...)`` fails. What those workflows *do* is asserted in
  ``tests/test_phase8.py``, which runs them against seeded journals.

    python scripts/run_examples.py                    # run them
    python scripts/run_examples.py --list             # just enumerate
    python scripts/run_examples.py examples/reference # only these
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docs_examples import ENVIRONMENTAL, is_environmental

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

#: Support files, not examples. ``utils`` and ``mock_http`` are imported by the
#: things that are; ``conftest`` belongs to pytest. Running them proves nothing
#: and failing on them would report a helper as a broken example.
SUPPORT = {"__init__.py", "utils.py", "conftest.py", "mock_http.py"}

#: Directories that hold no examples.
EXCLUDED_DIRS = {"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

MAIN_GUARD = '__name__ == "__main__"'
MAIN_GUARD_ALT = "__name__ == '__main__'"

#: An example may say how it wants to be run, on a line of its own near the top:
#:
#:     run-examples: --example 1
#:     run-examples: skip needs a terminal
#:
#: One example in the cookbook is an interactive CLI, and it is *correct* for it
#: to refuse an empty stdin. Special-casing that in the runner would put a file
#: name in the gate; a declared line keeps the knowledge next to the example,
#: where whoever edits it will see it.
DIRECTIVE = re.compile(r"^\s*#?\s*run-examples:\s*(.+?)\s*$", re.M)

#: How far into a file the directive is looked for — the module docstring and
#: the imports, not an incidental match a thousand lines down.
DIRECTIVE_SCAN_BYTES = 4000


@dataclass(frozen=True)
class Example:
    """One example file, and how it is meant to be exercised."""

    path: Path

    @property
    def label(self) -> str:
        return str(self.path.relative_to(ROOT))

    @property
    def directive(self) -> str | None:
        """What the file says about how to run it, if anything."""
        head = self.path.read_text(encoding="utf-8")[:DIRECTIVE_SCAN_BYTES]
        match = DIRECTIVE.search(head)
        return match.group(1) if match else None

    @property
    def is_script(self) -> bool:
        """Whether the file runs something when executed.

        A reference workflow defines a graph and returns; executing it as a
        script would exit 0 having proved only that the interpreter started.
        Importing it is what proves the decorators still accept what it passes
        them.
        """
        text = self.path.read_text(encoding="utf-8")
        return MAIN_GUARD in text or MAIN_GUARD_ALT in text

    @property
    def module(self) -> str:
        """Dotted name to import, relative to ``examples/``."""
        rel = self.path.relative_to(EXAMPLES).with_suffix("")
        return ".".join(rel.parts)


def discover(paths: list[Path] | None = None) -> list[Example]:
    """Every example under the given roots, or under ``examples/``."""
    roots = paths or [EXAMPLES]
    found: list[Example] = []
    for root in roots:
        candidates = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for path in candidates:
            if path.name in SUPPORT:
                continue
            if EXCLUDED_DIRS.intersection(path.parts):
                continue
            found.append(Example(path))
    return found


def run(example: Example, *, timeout: float) -> tuple[str, str]:
    """Execute or import one example. Returns ``(outcome, detail)``.

    A module is imported through ``-c`` with ``examples/`` on the path, so a
    reference workflow keeps its package name and can import a sibling. A
    script runs from its own directory, which is what puts ``utils`` on the
    cookbook's path without the examples needing to know they are being tested.
    """
    directive = example.directive
    if directive and directive.split(maxsplit=1)[0] == "skip":
        reason = directive.split(maxsplit=1)[1] if " " in directive else "declared"
        return "skipped", reason

    if example.is_script:
        argv = [sys.executable, str(example.path), *shlex.split(directive or "")]
        cwd = example.path.parent
    else:
        argv = [sys.executable, "-c", f"import {example.module}"]
        cwd = EXAMPLES

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "failed", f"did not finish within {timeout:.0f}s"

    if completed.returncode == 0:
        return "ok", ""

    # Both streams, because an example that stops for want of a credential says
    # so on stdout — `require_env` prints and exits. Reading stderr alone
    # reported the clearest failure in the cookbook as "no output".
    output = f"{completed.stderr}\n{completed.stdout}"
    lines = completed.stderr.strip().splitlines() or completed.stdout.strip().splitlines()
    if is_environmental(output):
        return "skipped", _cause(lines)
    return "failed", (lines or ["no output"])[-1]


def _cause(lines: list[str]) -> str:
    """The line that made this environmental, not the last line printed.

    A missing credential is reported and then explained over several lines, so
    the tail is the explanation's last sentence — true, and useless in a
    one-line summary.
    """
    for line in lines:
        if any(marker in line.lower() for marker in ENVIRONMENTAL):
            return line.strip()
    return (lines or ["no output"])[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories to run (default: every example)",
    )
    parser.add_argument("--list", action="store_true", help="enumerate, do not run")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a missing dependency or credential as a failure too",
    )
    args = parser.parse_args(argv)

    found = discover([p.resolve() for p in args.paths] or None)
    if args.list:
        for example in found:
            kind = "script" if example.is_script else "module"
            print(f"{kind:7} {example.label}")
        return 0

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    for example in found:
        outcome, detail = run(example, timeout=args.timeout)
        counts[outcome] += 1
        mark = {"ok": "ok  ", "skipped": "skip", "failed": "FAIL"}[outcome]
        print(f"  {mark}  {example.label}" + (f"  {detail[:90]}" if detail else ""))
        if outcome == "failed" or (args.strict and outcome == "skipped"):
            failures.append((example.label, detail))

    print(
        f"\n{counts['ok']} ran, {counts['skipped']} skipped "
        f"(missing dependency, service, or credential), {counts['failed']} failed"
    )
    if failures:
        print("\nFailures:")
        for label, detail in failures:
            print(f"  {label}\n    {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
