"""The append-only journal that makes executions durable and replayable.

Every durable operation is identified by a **path**: a dotted string like ``"3"`` or
``"3.1.0"``. Paths are allocated at the moment a call is *constructed* rather than when it
completes, which is what makes concurrency deterministic —
``asyncio.gather(ctx.step(a), ctx.step(b))`` evaluates its arguments left to right, so
``a`` is always ``"3"`` and ``b`` is always ``"4"`` no matter which finishes first.

Paths are hierarchical because composite operations nest. An agent run owns a scope, and
the model calls and tool calls inside it allocate ``"3.0"``, ``"3.1"``, and so on. Without
that nesting, replaying a completed agent (which short-circuits, and therefore never
re-allocates its children) would leave the cursor pointing at its children's numbers and
corrupt every subsequent operation.

On replay the workflow body re-executes from the top; each durable call consults its path
in the journal, and a completed entry returns the recorded value instead of running again.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from loom.core.exceptions import NondeterminismError
from loom.core.models import ErrorInfo, StepRecord, StepStatus, Usage

logger = logging.getLogger(__name__)


class EntryKind(StrEnum):
    """What kind of durable operation an entry represents."""

    STEP = "step"
    SIDE_EFFECT = "side_effect"
    """An inline non-deterministic read: ``ctx.now()``, ``ctx.uuid4()``, ``ctx.random()``."""
    AGENT = "agent"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    SLEEP = "sleep"
    EVENT = "event"
    CHILD_WORKFLOW = "child_workflow"
    SIGNAL = "signal"
    APPROVAL = "approval"


class EntryStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    """Terminal. Replay re-raises what the run originally saw."""
    EXHAUSTED = "exhausted"
    """The step spent its own retry budget; the *run* may still be attempted.

    A step that exhausts ``Retry`` has said nothing about whether the run is
    finished — a payment gateway returning 503 needs the same code run again in
    five minutes, not the journal edited. Recording that as ``FAILED`` makes it
    permanent, which leaves ``retry()`` (prune and restart) as the only
    recovery, and pruning throws away the attempt history that would tell an
    operator this has failed six times against the same gateway.

    Replay re-executes an exhausted entry as if it were absent, keeping the
    attempts recorded. The engine promotes it to ``FAILED`` when the run itself
    goes terminal and no outer driver claimed it.
    """
    SUSPENDED = "suspended"


def path_order(path: str) -> tuple[int, ...]:
    """Sort key that orders ``"2"`` before ``"10"`` and ``"3.1"`` after ``"3.0"``."""
    parts: list[int] = []
    for chunk in path.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class Scope:
    """Allocates sibling paths within one nesting level."""

    __slots__ = ("cursor", "prefix")

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.cursor = 0

    def allocate(self) -> str:
        path = f"{self.prefix}{self.cursor}"
        self.cursor += 1
        return path

    def child(self, path: str) -> Scope:
        """Open a nested scope beneath an already-allocated path."""
        return Scope(prefix=f"{path}.")

    def reset(self) -> None:
        self.cursor = 0

    def __repr__(self) -> str:
        return f"<Scope {self.prefix or '<root>'} cursor={self.cursor}>"


class JournalEntry(BaseModel):
    """One recorded durable operation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: str
    kind: EntryKind
    name: str
    status: EntryStatus = EntryStatus.PENDING
    fingerprint: str = ""
    contract_hash: str = ""
    """Hash of the step's input/output type annotations — detects contract changes."""
    closure_hash: str = ""
    """Hash of the step's function body — detects implementation changes."""
    idem_key: str = ""
    """Idempotency key derived from step arguments, for dedup across retries."""
    input: Any = None
    output: Any = None
    error: ErrorInfo | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    wake_at: datetime | None = None
    usage: Usage | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def depth(self) -> int:
        return self.path.count(".")

    @property
    def is_settled(self) -> bool:
        """Whether replay can serve this entry rather than re-running it.

        ``EXHAUSTED`` is deliberately absent: it is recorded, but it is not an
        answer, so counting it as settled would make ``Journal.replayed``
        over-report and let a lookup short-circuit work that still has to
        happen.
        """
        return self.status in (EntryStatus.COMPLETED, EntryStatus.FAILED)

    def to_record(self, seq: int) -> StepRecord:
        status_map = {
            EntryStatus.PENDING: StepStatus.PENDING,
            EntryStatus.COMPLETED: StepStatus.COMPLETED,
            EntryStatus.FAILED: StepStatus.FAILED,
            # Reads as failed to anything rendering a run — it did fail — while
            # the journal keeps the distinction that decides what replay does.
            EntryStatus.EXHAUSTED: StepStatus.FAILED,
            EntryStatus.SUSPENDED: StepStatus.RUNNING,
        }
        return StepRecord(
            seq=seq,
            name=self.name,
            kind=self.kind.value,
            status=StepStatus.CACHED if self.metadata.get("cache_hit") else status_map[self.status],
            fingerprint=self.fingerprint,
            input=self.input,
            output=self.output,
            error=self.error,
            attempts=self.attempts,
            started_at=self.started_at,
            finished_at=self.finished_at,
            usage=self.usage,
            metadata={**self.metadata, "path": self.path},
        )


class CompatibilityMode(StrEnum):
    """How replay reacts when the code no longer matches the recorded journal."""

    STRICT = "strict"
    """Raise :class:`NondeterminismError`. Correct for resuming production runs."""

    RESUME_FROM_DIVERGENCE = "resume_from_divergence"
    """Discard the journal from the first mismatch onward and keep executing.

    This is what powers "retry this failed run against the code I just fixed": everything
    before the edit is reused, everything after is recomputed.
    """


class VerifyMode(StrEnum):
    """Whether a replayed entry must prove it belongs to the call that found it.

    A path finds *an* entry. Kind and name make it plausible. Neither makes it
    right: two calls to one step at adjacent positions match each other's
    entries, so swapping the two lines replays each call against the other's
    recorded output — with no error, because everything the lookup compares is
    still equal. The fingerprint (name plus arguments) is the part that differs,
    and it has been recorded on every entry since the journal existed.

    Separate from :class:`CompatibilityMode`, which answers a different
    question. Compatibility is about *shape* — the workflow issued a different
    operation than the one recorded — and its answer is to truncate and move on.
    Verification is about *arguments*, where truncating would be far too
    destructive a response to what is usually a benign difference.
    """

    OFF = "off"
    """Compare nothing. What the engine did before verification existed."""

    WARN = "warn"
    """Serve the recorded value, log once, and flag the entry.

    The default, because an argument difference is not always a bug: a step
    whose input derives from ``ctx.state`` — which is deliberately not
    journaled — legitimately replays with different arguments. Raising on that
    would break correct workflows, so the first release only tells you.
    """

    STRICT = "strict"
    """Raise :class:`NondeterminismError` naming the step and both fingerprints."""


def _describe_hash_mismatch(
    entry: JournalEntry, contract_hash: str, closure_hash: str
) -> str:
    """Return a human-readable description if hashes diverge, else empty string."""
    parts: list[str] = []
    if contract_hash and entry.contract_hash and contract_hash != entry.contract_hash:
        parts.append(
            f"contract hash changed ({entry.contract_hash[:8]}→{contract_hash[:8]})"
        )
    if closure_hash and entry.closure_hash and closure_hash != entry.closure_hash:
        parts.append(
            f"closure hash changed ({entry.closure_hash[:8]}→{closure_hash[:8]})"
        )
    return "; ".join(parts)


class Journal:
    """In-memory view of one execution's recorded operations."""

    def __init__(
        self,
        entries: list[JournalEntry] | None = None,
        *,
        compatibility: CompatibilityMode = CompatibilityMode.STRICT,
        verify: VerifyMode = VerifyMode.WARN,
        resume_exhausted: bool = True,
    ) -> None:
        self._entries: dict[str, JournalEntry] = {e.path: e for e in entries or []}
        self._compatibility = compatibility
        self._verify = verify
        self.resume_exhausted = resume_exhausted
        """Whether an exhausted entry is re-executed or re-raised on this pass.

        The same record answers two different questions depending on who is
        asking. ``retry``/``resume`` want the step attempted again, with the
        work before it still served from the journal. ``replay`` is a rehearsal
        of what happened and must reproduce the failure the run actually saw.
        Reading the status per operation keeps one record honest for both,
        where mutating it on the way to terminal would settle the question for
        whichever asked first.
        """
        self._dirty: set[str] = set()
        self.root = Scope()
        self.replayed = 0
        self.executed = 0
        self.drifted: list[str] = []
        """Paths whose replayed arguments differed from the recorded ones."""

    # -- lookup -----------------------------------------------------------------------

    def get(self, path: str) -> JournalEntry | None:
        return self._entries.get(path)

    def lookup(
        self,
        path: str,
        kind: EntryKind,
        name: str,
        *,
        contract_hash: str = "",
        closure_hash: str = "",
        fingerprint: str = "",
    ) -> JournalEntry | None:
        """Return the entry at ``path``, verifying that replay has not diverged.

        Returns ``None`` when the operation is running for the first time, or when a
        divergence was tolerated under :attr:`CompatibilityMode.RESUME_FROM_DIVERGENCE`.

        When ``contract_hash`` or ``closure_hash`` are supplied, mismatches against the
        journal entry are treated as divergences — the step's signature or body changed.

        ``fingerprint`` identifies the call's *arguments*. It is checked under
        :class:`VerifyMode`, which is a separate axis from compatibility: a
        shape divergence truncates, an argument divergence warns or raises but
        never discards a journal the run may still need.
        """
        entry = self._entries.get(path)
        if entry is None:
            return None

        if entry.kind is kind and entry.name == name:
            self._verify_arguments(entry, fingerprint)
            # Check for contract/closure drift when hashes are available.
            hash_mismatch = _describe_hash_mismatch(entry, contract_hash, closure_hash)
            if hash_mismatch:
                if self._compatibility is CompatibilityMode.STRICT:
                    raise NondeterminismError(
                        f"replay diverged at position {path}: {hash_mismatch}. "
                        f"The step's contract or implementation changed since the journal was "
                        f"recorded. Replay with CompatibilityMode.RESUME_FROM_DIVERGENCE to "
                        f"re-execute from this point.",
                        seq=path_order(path)[0],
                        expected=f"{entry.kind.value}:{entry.name}",
                        actual=f"{kind.value}:{name}",
                    )
                self.truncate(path)
                return None

            if entry.is_settled:
                self.replayed += 1
            return entry

        expected = f"{entry.kind.value}:{entry.name}"
        actual = f"{kind.value}:{name}"
        if self._compatibility is CompatibilityMode.STRICT:
            raise NondeterminismError(
                f"replay diverged at position {path}: the journal recorded {expected} but the "
                f"workflow issued {actual}. Orchestration code must be deterministic — move "
                f"clocks, randomness, and I/O into steps, or replay with "
                f"CompatibilityMode.RESUME_FROM_DIVERGENCE.",
                seq=path_order(path)[0],
                expected=expected,
                actual=actual,
            )
        self.truncate(path)
        return None

    def _verify_arguments(self, entry: JournalEntry, fingerprint: str) -> None:
        """Check that a replayed call asked for what the recorded one asked for.

        Silent when either side has no fingerprint. An entry journaled before
        this check existed carries none, and refusing to replay those would
        make an upgrade strand every in-flight run — a far worse failure than
        the one being prevented.
        """
        if self._verify is VerifyMode.OFF:
            return
        if not fingerprint or not entry.fingerprint:
            return
        if fingerprint == entry.fingerprint:
            return

        if self._verify is VerifyMode.STRICT:
            raise NondeterminismError(
                f"replay diverged at position {entry.path}: {entry.kind.value} "
                f"'{entry.name}' was journaled with different arguments than the "
                f"call replaying now ({entry.fingerprint[:8]}→{fingerprint[:8]}). "
                f"Two calls to one step can swap places without changing their "
                f"kind or name, so the recorded value may belong to the other "
                f"call. If the arguments come from ctx.state or another "
                f"unjournaled read, move that read into a step; otherwise "
                f"replay with verify=VerifyMode.WARN.",
                seq=path_order(entry.path)[0],
                expected=entry.fingerprint,
                actual=fingerprint,
            )

        entry.metadata["argument_drift"] = True
        # Marked dirty so the flag reaches storage: a warning in a log the
        # operator was not watching is not a report. The write is idempotent
        # and happens only for entries that actually drifted.
        self._dirty.add(entry.path)
        self.drifted.append(entry.path)
        logger.warning(
            "replay argument drift at %s (%s '%s'): journaled %s, replaying %s. "
            "The recorded value is being served anyway. If the arguments come "
            "from ctx.state or another unjournaled read, move that read into a "
            "step so the run replays identically.",
            entry.path,
            entry.kind.value,
            entry.name,
            entry.fingerprint[:8],
            fingerprint[:8],
        )

    def find(self, kind: EntryKind, name: str) -> JournalEntry | None:
        """The first entry matching *kind* and *name*, wherever it sits.

        For the one record whose identity is its name rather than its position:
        a version gate has to be found by the same key after the code around it
        moves, or it cannot do its job.
        """
        for path in sorted(self._entries, key=path_order):
            entry = self._entries[path]
            if entry.kind is kind and entry.name == name:
                return entry
        return None

    def has_entries_after(self, path: str) -> bool:
        """Whether anything was recorded past *path*.

        Evidence that the body already ran through this position — which is how
        a version gate tells "never been here" from "was here before the gate
        existed".
        """
        boundary = path_order(path)
        return any(path_order(other) > boundary for other in self._entries)

    def truncate(self, path: str) -> None:
        """Drop the entry at ``path``, everything nested beneath it, and every later entry."""
        boundary = path_order(path)
        for key in [k for k in self._entries if path_order(k) >= boundary]:
            del self._entries[key]
            self._dirty.discard(key)

    # -- mutation ---------------------------------------------------------------------

    def mark_dirty(self, path: str) -> None:
        """Queue an already-recorded entry for re-persisting.

        For flags discovered *during* replay — argument drift, contract drift —
        which belong on the entry rather than only in a log the operator was
        not watching.
        """
        if path in self._entries:
            self._dirty.add(path)

    def put(self, entry: JournalEntry) -> JournalEntry:
        self._entries[entry.path] = entry
        self._dirty.add(entry.path)
        if entry.status is EntryStatus.COMPLETED:
            self.executed += 1
        return entry

    def drain_dirty(self) -> list[JournalEntry]:
        """Entries written since the last drain, for incremental persistence."""
        pending = [
            self._entries[path]
            for path in sorted(self._dirty, key=path_order)
            if path in self._entries
        ]
        self._dirty.clear()
        return pending

    # -- views ------------------------------------------------------------------------

    def entries(self) -> list[JournalEntry]:
        return [self._entries[path] for path in sorted(self._entries, key=path_order)]

    def records(self) -> list[StepRecord]:
        return [entry.to_record(index) for index, entry in enumerate(self.entries())]

    def total_usage(self) -> Usage:
        """Tokens and cost across the run.

        An agent entry already aggregates its own turns, so counting it *and*
        anything journalled beneath it would double-count. Nesting is visible in
        the path — a child of ``0002`` is ``0002.0000`` — so an agent's total is
        used only when it has no children of its own. Skipping agent entries
        outright, as this once did, reported zero tokens for every agent run,
        which is the main thing anyone wants the number for.
        """
        total = Usage()
        for entry in self._entries.values():
            if entry.usage is None:
                continue
            # An agent with journalled children is a rollup of them; count the
            # children instead. An agent with none is the only record there is.
            if entry.kind is EntryKind.AGENT and self._has_children(entry.path):
                continue
            total.add(entry.usage)
        return total

    def _has_children(self, path: str) -> bool:
        prefix = f"{path}."
        return any(other.startswith(prefix) for other in self._entries)

    def failed_entries(self) -> list[JournalEntry]:
        return [
            entry
            for entry in self.entries()
            if entry.status in (EntryStatus.FAILED, EntryStatus.EXHAUSTED)
        ]

    def exhausted_entries(self) -> list[JournalEntry]:
        """Entries a further attempt at the run would re-execute."""
        return [
            entry for entry in self.entries() if entry.status is EntryStatus.EXHAUSTED
        ]

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"<Journal entries={len(self._entries)}>"
