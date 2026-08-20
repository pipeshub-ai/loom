"""Fixes a machine can make, so a model is not asked to make them.

A repair round costs a model call, several seconds, and — because a reply that
changes anything is a reply that did not *decline* — it also destroys the
signal that lets an advisory finding be accepted. Spending one on deleting an
unused import is a bad trade twice over.

Worse, the SDK manufactures that particular finding. The system prompt says, in
as many words, "Start the file with: ``from loom import Context, Retry, step,
workflow``". A workflow that needs no retry policy therefore imports ``Retry``,
does not use it, and ruff reports ``F401`` as a blocking error — a defect
created by following the instructions.

So the deterministic ones are applied here, before the pipeline sees the code.
Only fixes ruff itself marks **safe**, and only from a named rule set: this
rewrites what a model wrote, and the licence for that is that the change cannot
alter behaviour. Anything requiring judgement stays a finding for the model.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TIDY_RULES", "Tidied", "tidy"]

#: What is safe to fix without asking. Deliberately short.
#:
#: ``F401`` unused import — behaviour-preserving by definition, and the rule the
#: prompt's own mandated import line keeps triggering.
#: ``F541`` f-string with no placeholders — a literal either way.
#: ``UP035``/``UP008`` deprecated import and ``super()`` forms — mechanical.
#:
#: Nothing here can change what the workflow *does*. A rule that could belongs
#: in the pipeline as a finding, where a model gets to disagree with it.
TIDY_RULES: tuple[str, ...] = ("F401", "F541", "UP035", "UP008")


@dataclass(frozen=True)
class Tidied:
    """The code after mechanical fixes, and what was mechanical about it."""

    code: str
    fixed: int = 0
    """How many findings ruff resolved."""
    skipped: str = ""
    """Why nothing was attempted, when nothing was — an absent linter, say.

    Reported rather than silent, on the pipeline's own rule: a step that could
    not run has not done anything, and pretending otherwise hides why a finding
    the model then has to fix by hand reached it.
    """

    @property
    def changed(self) -> bool:
        return self.fixed > 0


def tidy(code: str, *, timeout: float = 20.0) -> Tidied:
    """Apply ruff's safe fixes for :data:`TIDY_RULES` to *code*.

    Returns the code unchanged, with a reason, when ruff is not installed or
    cannot run. Never raises: this sits in front of the verification pipeline,
    and a tidier that fails must not be the reason a generation produces
    nothing.
    """
    executable = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    if not Path(executable).exists():
        return Tidied(code=code, skipped="ruff is not installed")

    try:
        completed = subprocess.run(
            [
                executable,
                "check",
                "--fix",
                "--select",
                ",".join(TIDY_RULES),
                # Safe fixes only. `--unsafe-fixes` is exactly the set whose
                # behaviour ruff will not vouch for, which is the line this
                # module must not cross.
                "--no-unsafe-fixes",
                "--stdin-filename",
                "generated.py",
                "-",
            ],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Tidied(code=code, skipped=f"ruff failed: {exc}")

    fixed_code = completed.stdout
    if not fixed_code.strip():
        # ruff writes the file to stdout even when it changes nothing; empty
        # output means something went wrong, and the original is the safe answer.
        return Tidied(code=code, skipped="ruff produced no output")

    return Tidied(code=fixed_code, fixed=_fix_count(completed.stderr))


def _fix_count(stderr: str) -> int:
    """How many findings ruff says it resolved, from its summary line."""
    import re

    found = re.search(r"(\d+) fixed", stderr or "")
    return int(found.group(1)) if found else 0
