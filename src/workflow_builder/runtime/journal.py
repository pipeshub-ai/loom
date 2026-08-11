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

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from workflow_builder.core.exceptions import NondeterminismError
from workflow_builder.core.models import ErrorInfo, StepRecord, StepStatus, Usage


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
        return self.status in (EntryStatus.COMPLETED, EntryStatus.FAILED)

    def to_record(self, seq: int) -> StepRecord:
        status_map = {
            EntryStatus.PENDING: StepStatus.PENDING,
            EntryStatus.COMPLETED: StepStatus.COMPLETED,
            EntryStatus.FAILED: StepStatus.FAILED,
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
    ) -> None:
        self._entries: dict[str, JournalEntry] = {e.path: e for e in entries or []}
        self._compatibility = compatibility
        self._dirty: set[str] = set()
        self.root = Scope()
        self.replayed = 0
        self.executed = 0

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
    ) -> JournalEntry | None:
        """Return the entry at ``path``, verifying that replay has not diverged.

        Returns ``None`` when the operation is running for the first time, or when a
        divergence was tolerated under :attr:`CompatibilityMode.RESUME_FROM_DIVERGENCE`.

        When ``contract_hash`` or ``closure_hash`` are supplied, mismatches against the
        journal entry are treated as divergences — the step's signature or body changed.
        """
        entry = self._entries.get(path)
        if entry is None:
            return None

        if entry.kind is kind and entry.name == name:
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

    def truncate(self, path: str) -> None:
        """Drop the entry at ``path``, everything nested beneath it, and every later entry."""
        boundary = path_order(path)
        for key in [k for k in self._entries if path_order(k) >= boundary]:
            del self._entries[key]
            self._dirty.discard(key)

    # -- mutation ---------------------------------------------------------------------

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
        total = Usage()
        for entry in self._entries.values():
            # Composite entries aggregate their children, so counting both double-counts.
            if entry.usage is not None and entry.kind is not EntryKind.AGENT:
                total.add(entry.usage)
        return total

    def failed_entries(self) -> list[JournalEntry]:
        return [entry for entry in self.entries() if entry.status is EntryStatus.FAILED]

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"<Journal entries={len(self._entries)}>"
