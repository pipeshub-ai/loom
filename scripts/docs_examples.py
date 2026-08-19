"""Extract and execute the Python examples in the documentation.

Running an example is the only check that catches a *wrong* API. Missing
imports fail a linter and bad syntax fails a compiler, but
``Retry(backoff=2.0)`` resolves every name, compiles cleanly, and raises
``TypeError`` the moment anyone follows the docs — which reads as a broken
library rather than a stale page.

Executing them needs optional extras, database services, and in a few cases an
API key, so this runs as its own CI job rather than in the default suite. The
extraction is shared with ``tests/test_docs_examples.py`` so both check the same
thing.

    python scripts/docs_examples.py            # run them
    python scripts/docs_examples.py --list     # just enumerate
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"```python\n(.*?)```", re.S)

#: A page may declare imports its examples share, rather than repeating them in
#: every block. Prepended to each block on that page.
PREAMBLE_MARKER = "<!-- docs-preamble -->"

#: A page may declare that its blocks are illustrations rather than examples.
#:
#: Design and planning documents quote code that does not exist yet, or quote a
#: fragment of code that does, to argue about it. Neither is something a reader
#: copies, and holding them to the same bar as a guide has one of two outcomes:
#: the check goes red and stops being trusted, or the argument gets contorted
#: into runnable snippets and stops being clear. The marker is per-page and
#: explicit, so a real guide cannot opt out by accident.
SKIP_MARKER = "<!-- docs-illustrative -->"

#: Failures that are about the machine, not the documentation. An example that
#: needs a database is not wrong because this runner has no database; saying so
#: is the difference between a useful signal and a red build nobody trusts.
ENVIRONMENTAL = (
    "no module named 'motor'",
    "no module named 'asyncpg'",
    "no module named 'langchain",
    "no module named 'agno'",
    "no module named 'pydantic_ai'",
    "no module named 'anthropic'",
    "no module named 'openai'",
    "api key",
    "api_key",
    "anthropic_api_key",
    "openai_api_key",
    "connection refused",
    "could not connect",
    "serverselectiontimeout",
    "getaddrinfo",
    "name or service not known",
    "temporary failure in name resolution",
    # What `examples/cookbook/utils.py::require_env` prints before exiting. An
    # example that stops because nobody gave it a Google refresh token has not
    # found anything wrong with itself.
    "missing env vars",
)


@dataclass(frozen=True)
class Example:
    """One documented code block."""

    path: Path
    line: int
    code: str

    @property
    def label(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}"


def markdown_files() -> list[Path]:
    """The documents a reader copies from."""
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    return [path for path in files if path.exists()]


def preamble_of(text: str) -> str:
    """The shared imports a page declares, or ``""``."""
    marker = text.find(PREAMBLE_MARKER)
    if marker == -1:
        return ""
    match = BLOCK.search(text, marker)
    return match.group(1) if match else ""


def examples(paths: list[Path] | None = None) -> list[Example]:
    """Every example, each carrying its page's shared imports.

    Pages marked :data:`SKIP_MARKER` contribute nothing: their blocks are
    illustrations in an argument, not code anyone runs.
    """
    found: list[Example] = []
    for path in paths or markdown_files():
        text = path.read_text(encoding="utf-8")
        if SKIP_MARKER in text:
            continue
        shared = preamble_of(text)
        for match in BLOCK.finditer(text):
            body = match.group(1)
            code = body if body == shared else f"{shared}\n{body}"
            found.append(
                Example(path, text[: match.start()].count("\n") + 1, code)
            )
    return found


def is_environmental(output: str) -> bool:
    """True when the failure is a missing dependency, service, or credential."""
    lowered = output.lower()
    return any(marker in lowered for marker in ENVIRONMENTAL)


def run(example: Example, *, timeout: float) -> tuple[str, str]:
    """Execute one example. Returns ``(outcome, detail)``."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "example.py"
        script.write_text(example.code, encoding="utf-8")
        try:
            completed = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmp,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "failed", f"did not finish within {timeout:.0f}s"

    if completed.returncode == 0:
        return "ok", ""

    if completed.returncode < 0:
        # Stopped from outside rather than failing: a signal, usually the OOM
        # killer on a loaded machine. Its stderr is whatever had been flushed
        # when it died, so judging the example by it reports a defect that is
        # not there and hides the one that is.
        return "skipped", f"killed by signal {-completed.returncode}"

    detail = (completed.stderr.strip().splitlines() or ["no output"])[-1]
    if is_environmental(completed.stderr):
        return "skipped", detail
    return "failed", detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="enumerate, do not run")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a missing dependency or credential as a failure too",
    )
    args = parser.parse_args(argv)

    found = examples()
    if args.list:
        for example in found:
            print(example.label)
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
