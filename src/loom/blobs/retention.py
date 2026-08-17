"""Retention policies and compaction for durable state.

Controls how long completed run data is kept before archival or deletion.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from loom.core.models import ExecutionStatus

#: Rows per store round trip while scanning for old runs.
_COMPACT_PAGE = 500

#: Only these can be compacted — a suspended run is still going to resume.
TERMINAL_STATUSES: tuple[ExecutionStatus, ...] = (
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
)


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _blob_ref(payload: Any) -> str | None:
    """The blob reference in a journal payload, if it was offloaded."""
    from loom.runtime.context import BLOB_KEY

    if isinstance(payload, dict) and set(payload) == {BLOB_KEY}:
        ref = payload[BLOB_KEY]
        return ref if isinstance(ref, str) else None
    return None


#: How far down a chain of replay clones to look. `Runtime.replay` names a
#: clone `<run_id>:replay`, and replaying a clone appends again, so the chain is
#: discoverable by name without a store query for descendants.
_CLONE_DEPTH = 8


async def _refs_held_by_clones(store: Any, run_id: str) -> set[str]:
    """Blob refs a surviving replay clone of *run_id* still needs.

    `Runtime.replay` copies the source journal verbatim, so the clone's entries
    name the same blobs. Deleting them while compacting the source leaves the
    clone with a journal it cannot read — the one way this failure reaches a
    correctly configured deployment, since the sharing is created by LOOM rather
    than by chance.

    Costs one journal read per surviving clone, and clones are rare.
    """
    held: set[str] = set()
    name = run_id
    for _ in range(_CLONE_DEPTH):
        name = f"{name}:replay"
        try:
            entries = await store.load_journal(name)
        except Exception:
            # A missing clone is the common case, not a fault.
            break
        if not entries:
            break
        for entry in entries:
            ref = _blob_ref(entry.output)
            if ref is not None:
                held.add(ref)
    return held


class RetentionPolicy(BaseModel):
    """Configures how long each data category is retained before compaction."""

    journal_hot_days: int = 7
    journal_warm_days: int = 90
    payload_hot_days: int = 7
    payload_warm_days: int = 30
    run_record_days: int = 365


_CATEGORY_FIELD: dict[str, str] = {
    "journal_hot": "journal_hot_days",
    "journal_warm": "journal_warm_days",
    "payload_hot": "payload_hot_days",
    "payload_warm": "payload_warm_days",
    "run_record": "run_record_days",
}


class CompactionResult(BaseModel):
    """Summary of a single compaction pass."""

    journals_archived: int = 0
    payloads_deleted: int = 0
    runs_archived: int = 0
    kv_expired: int = 0

    scan_truncated: bool = False
    """The pass stopped at ``scan_limit`` with rows left unexamined.

    Deliberately visible on the result rather than logged and forgotten: every
    other field here can read ``0`` both because there was nothing to do and
    because the scan never reached it, and those call for opposite responses.
    Not counted in :attr:`total` — it is a caveat about the count, not an item.
    """

    @property
    def total(self) -> int:
        """Total number of items affected by compaction."""
        return (
            self.journals_archived
            + self.payloads_deleted
            + self.runs_archived
            + self.kv_expired
        )


class RetentionManager:
    """Applies a :class:`RetentionPolicy` to a store.

    Compaction is deliberately two-staged, because the two costs are different:
    a journal is large and stops being interesting once a run is old, while the
    run record is small and is what someone searching for "did that ever run?"
    actually finds. So journals are dropped first, and the record itself only
    much later.
    """

    def __init__(self, policy: RetentionPolicy) -> None:
        self._policy = policy

    def cutoff_date(self, category: str) -> datetime:
        """Return the cutoff datetime for *category*.

        Supported categories: ``journal_hot``, ``journal_warm``,
        ``payload_hot``, ``payload_warm``, ``run_record``.

        Raises:
            ValueError: If *category* is not recognised.
        """
        field = _CATEGORY_FIELD.get(category)
        if field is None:
            raise ValueError(
                f"unknown category '{category}'; "
                f"expected one of {sorted(_CATEGORY_FIELD)}"
            )
        days: int = getattr(self._policy, field)
        return datetime.now(UTC) - timedelta(days=days)

    async def compact(
        self,
        store: Any,
        *,
        blobs: Any | None = None,
        batch_size: int = 500,
        scan_limit: int = 50_000,
        dry_run: bool = False,
    ) -> CompactionResult:
        """Run one compaction pass against *store*.

        Only terminal runs are touched — a suspended run parked on a
        three-week timer is not stale, however old its ``created_at`` is, and
        dropping its journal would destroy an execution that is still going to
        resume.

        Parameters
        ----------
        blobs:
            A :class:`~loom.blobs.blob.BlobService`. When given,
            blobs referenced only by the journals being dropped are deleted too
            — otherwise blob storage grows forever while the runs that explain
            it are compacted away.
        batch_size:
            How many runs to *compact* per status, not how many to look at.
            Compaction is meant to be run repeatedly from a scheduler, not to
            drain everything at once.

            The distinction is the correctness of this method. ``finished_at``
            lives inside the record's JSON payload, so the cutoff cannot be a
            ``WHERE`` clause and is applied here — and it used to be applied to
            a single ``list_executions(limit=batch_size)``, which orders
            newest-first because ``run_id`` is a ULID. Old runs are exactly the
            ones being compacted, so they sat behind every recent run and never
            entered the window: a store with more than ``batch_size`` terminal
            runs reported ``runs_archived=0`` forever while growing without
            bound, and the documented reclamation path looked like it was
            working.
        scan_limit:
            Rows examined per status before the pass gives up and says so. A
            pass that quietly stops early is the same defect one page larger.
        dry_run:
            Count what would be affected without deleting anything.
        """
        journal_cutoff = self.cutoff_date("journal_warm")
        record_cutoff = self.cutoff_date("run_record")
        result = CompactionResult()

        for status in TERMINAL_STATUSES:
            for record in await self._stale_records(
                store,
                status,
                journal_cutoff=journal_cutoff,
                record_cutoff=record_cutoff,
                batch_size=batch_size,
                scan_limit=scan_limit,
                result=result,
            ):
                finished = _as_utc(record.finished_at or record.created_at)
                expiring = finished < record_cutoff

                # Collect blob refs before the journal that names them is gone.
                if blobs is not None:
                    result.payloads_deleted += await self._drop_blobs(
                        store, blobs, record.run_id, dry_run=dry_run
                    )
                if not dry_run:
                    await self._drop_staging(store, record.run_id)

                if expiring:
                    if not dry_run:
                        await store.delete_execution(record.run_id)
                    result.runs_archived += 1
                    # The journal went with it; don't count it twice.
                    continue

                if not dry_run:
                    # Paths sort lexicographically, so "" cuts from the start.
                    await store.truncate_journal(record.run_id, "")
                result.journals_archived += 1

        return result

    @staticmethod
    async def _stale_records(
        store: Any,
        status: ExecutionStatus,
        *,
        journal_cutoff: datetime,
        record_cutoff: datetime,
        batch_size: int,
        scan_limit: int,
        result: CompactionResult,
    ) -> list[Any]:
        """Runs of *status* old enough to compact, found by paging the store.

        Stops at ``batch_size`` matches so one pass stays bounded, and at
        ``scan_limit`` rows examined so a very large store cannot make a pass
        unbounded either. The second ceiling is recorded on the result rather
        than swallowed — a caller that sees ``runs_archived=0`` needs to know
        whether that means "nothing was old enough" or "we did not get that far".
        """
        # `finished_at` became an indexed column in migration 1, so the stores
        # that ship with LOOM answer this without a scan. A host's own store
        # need not implement it; the paging loop below is correct, just linear.
        indexed = getattr(store, "terminal_before", None)
        if indexed is not None:
            cutoff = max(journal_cutoff, record_cutoff)
            return list(await indexed(cutoff, [status], limit=batch_size))

        found: list[Any] = []
        scanned = 0
        offset = 0
        while len(found) < batch_size:
            if scanned >= scan_limit:
                if await store.list_executions(status=status, limit=1, offset=offset):
                    result.scan_truncated = True
                break
            page = await store.list_executions(
                status=status,
                limit=min(_COMPACT_PAGE, scan_limit - scanned),
                offset=offset,
            )
            if not page:
                break
            scanned += len(page)
            offset += len(page)
            for record in page:
                finished = record.finished_at or record.created_at
                if finished is None:
                    continue
                finished = _as_utc(finished)
                # Old enough to lose its journal, or old enough to lose the
                # record entirely. Anything newer than both is left alone.
                if finished < record_cutoff or finished < journal_cutoff:
                    found.append(record)
                    if len(found) >= batch_size:
                        break
        return found

    @staticmethod
    async def _drop_blobs(
        store: Any, blobs: Any, run_id: str, *, dry_run: bool
    ) -> int:
        """Delete blobs referenced by one run's journal. Returns the count.

        Deletion used to be justified as best-effort — "a blob shared with a
        live run has already been removed by whichever run got here first, and
        the second delete is a harmless no-op". That reasons about deleting
        twice, not about deleting something still needed. Blobs are
        content-addressed, so sharing is the *normal* case for a deterministic
        step, and `Runtime.replay` guarantees it: the clone's journal is a copy
        of the original's, sharing every ref. Compacting the original left the
        clone unable to replay at all.

        A general fix is a refcount, which needs a table this store does not
        have. What is closed here is the sharing LOOM itself creates, which is
        both the common case and the only one that is certain: refs still named
        by a surviving replay clone are kept.
        """
        held = await _refs_held_by_clones(store, run_id)
        deleted = 0
        for entry in await store.load_journal(run_id):
            ref = _blob_ref(entry.output)
            if ref is None or ref in held:
                continue
            deleted += 1
            if not dry_run:
                with suppress(Exception):
                    await blobs.delete(ref)
        return deleted

    @staticmethod
    async def _drop_staging(store: Any, run_id: str) -> None:
        """Drop per-run staging entries once the run itself is being compacted."""
        manifest_key = f"staging:{run_id}:__manifest__"
        names = await store.get(manifest_key)
        if not names:
            return
        for name in names:
            with suppress(Exception):
                await store.delete(f"staging:{run_id}:{name}")
        with suppress(Exception):
            await store.delete(manifest_key)

    def should_archive_run(self, completed_at: datetime) -> bool:
        """Return ``True`` if a run completed before the run-record cutoff."""
        cutoff = datetime.now(UTC) - timedelta(days=self._policy.run_record_days)
        return completed_at < cutoff

    def should_archive_journal(self, completed_at: datetime) -> bool:
        """Return ``True`` if journal entries should move to warm/cold storage."""
        cutoff = datetime.now(UTC) - timedelta(days=self._policy.journal_warm_days)
        return completed_at < cutoff
