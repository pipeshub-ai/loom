"""Run mypy, and fail if it did not actually check anything.

mypy has a failure mode that reads exactly like a clean-ish run, and this
project walks into it by default. Under an ``[all]`` environment the optional
integration SDKs pull numpy transitively; numpy's stubs use PEP 695 ``type``
statements, which are a **syntax** error below 3.12, so mypy cannot parse them
and stops with::

    Found 4 errors in 3 files (errors prevented further checking)

Four errors, none of them yours, exit code 1 — and **nothing in this codebase
was type-checked**. The signal is one word in the last line, and the difference
between it and ``(checked 397 source files)`` is the difference between a gate
and a decoration.

That is not hypothetical: it is how 42 real errors across three new toolsets
went unnoticed. Running ``mypy`` in the working environment reported the numpy
and playwright lines and looked like somebody else's problem.

So this wrapper asserts the tail. mypy's summary is one of:

    Success: no issues found in N source files
    Found X errors in Y files (checked N source files)
    Found X errors in Y files (errors prevented further checking)   <-- the trap

Only the first two mean the gate ran. The third exits **2** with an
explanation, so it is distinguishable from ordinary type errors (exit 1) by
anything reading the exit code rather than the text.

    python scripts/typecheck.py            # what CI runs
    python scripts/typecheck.py --strict-env   # also refuse a numpy-bearing env
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

#: mypy's own words when it gave up before finishing.
GAVE_UP = "errors prevented further checking"

#: The two summaries that mean a real pass over the tree happened.
CHECKED = re.compile(
    r"(?:Success: no issues found in (\d+) source files?"
    r"|\(checked (\d+) source files?\))"
)

#: Below this, mypy checked so little that something is misconfigured — a
#: narrowed `packages`, a bad `mypy_path`. LOOM is ~400 files; a run over 20 is
#: not the gate anybody thinks they are running.
MINIMUM_FILES = 100

ADVICE = """
mypy stopped before type-checking this codebase, so nothing here was verified.

The usual cause is the environment. `[tool.mypy] python_version = "3.11"` is the
floor `requires-python` promises, and checking against the oldest supported
version is the point — but the optional integration SDKs pull numpy in, and
numpy's stubs use PEP 695 `type` statements, which are a *syntax* error below
3.12. mypy cannot parse them and gives up on everything.

Type-check in a `[dev]` environment, which pulls none of them:

    python -m venv .venv-typecheck
    .venv-typecheck/bin/pip install -e ".[dev]"
    .venv-typecheck/bin/python scripts/typecheck.py

The full test suite still needs `[all]`. That is a separate environment, on
purpose — see the long comment above `[tool.mypy]` in pyproject.toml.
"""


def run(argv: list[str]) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "mypy", *argv],
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    print(output, end="" if output.endswith("\n") else "\n")

    if GAVE_UP in output:
        print(ADVICE, file=sys.stderr)
        return 2

    match = CHECKED.search(output)
    if match is None:
        print(
            "\ntypecheck: mypy printed no 'checked N source files' summary, so "
            "there is no evidence it ran over the tree. Treating that as a "
            "failure rather than a pass.",
            file=sys.stderr,
        )
        print(ADVICE, file=sys.stderr)
        return 2

    counted = int(match.group(1) or match.group(2))
    if counted < MINIMUM_FILES:
        print(
            f"\ntypecheck: mypy checked only {counted} source files, well under "
            f"the {MINIMUM_FILES} this codebase has. Something has narrowed the "
            "check — `packages`, `mypy_path`, or a stray argument — so it is "
            "not the gate it looks like.",
            file=sys.stderr,
        )
        return 2

    # Ordinary type errors keep mypy's own exit code, so this is a drop-in.
    return completed.returncode


def numpy_present() -> bool:
    """Whether the trap's usual cause is installed here."""
    from importlib.util import find_spec

    try:
        return find_spec("numpy") is not None
    except (ImportError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict-env",
        action="store_true",
        help="refuse to run at all when numpy is importable",
    )
    known, passthrough = parser.parse_known_args(argv)

    if known.strict_env and numpy_present():
        print(
            "typecheck: numpy is importable here, which means this is not a "
            "`[dev]` environment and mypy will stop before checking anything.",
            file=sys.stderr,
        )
        print(ADVICE, file=sys.stderr)
        return 2

    return run(passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
