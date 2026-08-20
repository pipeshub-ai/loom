"""Turn a real run into a regression test.

The loop that makes durable execution pay for itself, and the one piece of it
that was missing. LOOM has better raw material than any comparable platform — a
complete, replayable record of every durable operation — and it had two ways to
look at that record (``loom show``, ``loom watch``) and no way to *keep* it. The
mechanism was already here: :func:`~loom.testing.journal.given` seeds a journal
entry, and a seeded entry means exactly what a recorded one means, so there is
nothing to keep in sync. Nothing connected a production failure to it.

``loom pin <run>`` closes that: one command turns a failure into a committed
test that fails for the same reason, and passes when it is fixed. It is n8n's
"pin data / debug in editor" with a durable journal underneath instead of a
copy of the trigger payload.

**Secrets do not leave here.** A step's *inputs* are already redacted on the way
into the journal, but its *outputs* are not — they are the values the step
produced, and nobody writing a workflow expected them to be pasted into a file
somebody commits. Everything this module emits goes through
:mod:`loom.core.redaction` first.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import Any

from loom.core.redaction import DEFAULT_REDACT_KEYS, REDACTED, redact
from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry

__all__ = ["PinnedTest", "pin_run"]

#: Entry kinds worth seeding. A `side_effect` (`ctx.now()`, `ctx.uuid4()`) is
#: journaled so it replays identically and is *served* automatically by the
#: replay engine, so seeding one adds nothing; a `sleep` costs nothing to
#: re-take under a ManualClock.
_SEEDABLE = frozenset({
    EntryKind.STEP,
    EntryKind.AGENT,
    EntryKind.CHILD_WORKFLOW,
    EntryKind.TOOL_CALL,
})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NODE_PREFIX = "node:"


@dataclass(frozen=True)
class PinnedTest:
    """A generated regression test, and what it needed to guess at."""

    source: str
    """The pytest module, ready to write."""
    filename: str
    """A conventional name for it."""
    seeded: int
    """How many journal entries became ``given(...)`` declarations."""
    notes: list[str]
    """What the generator could not determine, for a human to finish.

    Reported rather than silently filled in: a test whose imports are a guess
    fails for a reason that has nothing to do with the run it was pinning, and
    the reader has no way to tell which.
    """


def pin_run(
    record: Any,
    journal: list[JournalEntry],
    *,
    module: str = "",
    workflow_symbol: str = "",
) -> PinnedTest:
    """Build a regression test that reproduces *record* from its journal.

    Args:
        record: The ``ExecutionRecord`` to pin.
        journal: Its entries, in order.
        module: Import path holding the workflow and its steps, e.g.
            ``flows.digest``. Left empty, the test carries a ``TODO`` naming
            what is missing rather than importing something that may not exist.
        workflow_symbol: The Python name of the workflow function, when it
            differs from the workflow's registered name.
    """
    notes: list[str] = []
    workflow_name = getattr(record, "workflow", "workflow")
    symbol = workflow_symbol or _as_identifier(workflow_name)
    if symbol != workflow_name:
        notes.append(
            f"workflow registered as {workflow_name!r}; assuming the Python "
            f"symbol is {symbol}"
        )

    seeds = [
        seed
        for seed in (_seed_for(entry, notes) for entry in journal)
        if seed is not None
    ]

    if _was_redacted(getattr(record, "input", None)) or any(
        _was_redacted(seed.returns) for seed in seeds
    ):
        notes.append(
            "one or more values were redacted, so the test replays with '***' "
            "where the run had a credential — substitute a fixture value if the "
            "body actually reads it"
        )
    step_symbols = sorted({seed.symbol for seed in seeds if seed.symbol})

    if not module:
        notes.append(
            "no module given — fill in the import, or re-run with "
            "--module <import.path>"
        )

    return PinnedTest(
        source=_render(
            record=record,
            workflow_name=workflow_name,
            symbol=symbol,
            module=module,
            step_symbols=step_symbols,
            seeds=seeds,
            notes=notes,
        ),
        filename=f"test_pinned_{_slug(getattr(record, 'run_id', 'run'))}.py",
        seeded=len(seeds),
        notes=notes,
    )


@dataclass(frozen=True)
class _Seed:
    """One ``given(...)`` line, prepared."""

    symbol: str
    name: str
    kind: EntryKind
    occurrence: int
    returns: Any = None
    raises: str = ""

    def render(self) -> str:
        target = self.symbol or repr(self.name)
        parts = [target]
        if self.raises:
            parts.append(f"raises={self.raises}")
        else:
            parts.append(f"returns={self.returns!r}")
        if self.occurrence:
            parts.append(f"occurrence={self.occurrence}")
        if self.kind is not EntryKind.STEP:
            parts.append(f"kind=EntryKind.{self.kind.name}")
        return f"given({', '.join(parts)})"


def _seed_for(entry: JournalEntry, notes: list[str]) -> _Seed | None:
    """A seed for one entry, or ``None`` when it does not need one."""
    if entry.kind not in _SEEDABLE:
        return None
    if entry.status not in (
        EntryStatus.COMPLETED,
        EntryStatus.FAILED,
        EntryStatus.EXHAUSTED,
    ):
        return None

    name = entry.name
    if name.startswith(_NODE_PREFIX):
        # A node is resolved from the catalogue at call time, so there is no
        # Python symbol to import; seeding it by name is the honest form.
        notes.append(f"{name} is a node — seeded by name, not by symbol")
        symbol = ""
    else:
        symbol = name if _IDENTIFIER.match(name) and not keyword.iskeyword(name) else ""
        if not symbol:
            notes.append(f"{name!r} is not a Python identifier — seeded by name")

    if entry.error is not None and entry.status is not EntryStatus.COMPLETED:
        return _Seed(
            symbol=symbol,
            name=name,
            kind=entry.kind,
            occurrence=0,
            raises=f"RuntimeError({entry.error.message!r})",
        )

    return _Seed(
        symbol=symbol,
        name=name,
        kind=entry.kind,
        occurrence=0,
        returns=redact(entry.output, DEFAULT_REDACT_KEYS),
    )


def _render(
    *,
    record: Any,
    workflow_name: str,
    symbol: str,
    module: str,
    step_symbols: list[str],
    seeds: list[_Seed],
    notes: list[str],
) -> str:
    """The pytest module."""
    run_id = getattr(record, "run_id", "unknown")
    status = getattr(getattr(record, "status", None), "value", "unknown")
    payload = redact(getattr(record, "input", None), DEFAULT_REDACT_KEYS)

    imports = ", ".join(dict.fromkeys([symbol, *step_symbols]))
    import_line = (
        f"from {module} import {imports}"
        if module
        else f"# TODO: from <your.module> import {imports}"
    )

    # Numbered so a repeated step lands on the right call. `given` resolves by
    # name and kind, so only the occurrence is ordinal — and it has to count
    # per name, not per entry.
    occurrences: dict[str, int] = {}
    lines: list[str] = []
    for seed in seeds:
        index = occurrences.get(seed.name, 0)
        occurrences[seed.name] = index + 1
        numbered = _Seed(
            symbol=seed.symbol,
            name=seed.name,
            kind=seed.kind,
            occurrence=index,
            returns=seed.returns,
            raises=seed.raises,
        )
        lines.append(f"        {numbered.render()},")

    note_block = (
        "\n".join(f"#   - {note}" for note in notes)
        if notes
        else "#   (nothing needed guessing)"
    )
    kinds_import = (
        "from loom.runtime.journal import EntryKind\n"
        if any(seed.kind is not EntryKind.STEP for seed in seeds)
        else ""
    )

    return f'''"""Regression test pinned from run {run_id}.

Generated by ``loom pin``. Every value below came from that run's journal — a
seeded entry means exactly what a recorded one means, so this reproduces what
happened rather than approximating it.

Values are redacted on the way out (``loom.core.redaction``), so a credential a
step returned is not in this file. Check that before committing anyway.

What the generator had to guess at:
{note_block}
"""

from __future__ import annotations

import pytest

from loom.core.models import ExecutionStatus
{kinds_import}from loom.testing import given, run_with

{import_line}

#: The input the run was started with.
RUN_INPUT = {payload!r}


@pytest.mark.asyncio
async def test_{_slug(workflow_name)}_reproduces_{_slug(run_id)}() -> None:
    """Replays run {run_id}, which finished {status}."""
    result = await run_with(
        {symbol},
        RUN_INPUT,
{chr(10).join(lines) if lines else "        # no seedable entries in that journal"}
    )

    assert result.status is ExecutionStatus.{status.upper()}
'''


def _as_identifier(name: str) -> str:
    """A plausible Python symbol for a registered workflow name."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"flow_{cleaned}"
    return cleaned


def _slug(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower() or "run"


def _was_redacted(value: Any) -> bool:
    """Whether redaction actually replaced something in *value*.

    Reported rather than assumed. A pinned test whose input silently became
    ``'***'`` reproduces a *different* run from the one it claims to, and the
    reader has no way to tell — so this says when that has happened, which is
    the only honest form a safety measure that changes the data can take.
    """
    if isinstance(value, str):
        return value == REDACTED
    if isinstance(value, dict):
        return any(_was_redacted(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_was_redacted(item) for item in value)
    return False
