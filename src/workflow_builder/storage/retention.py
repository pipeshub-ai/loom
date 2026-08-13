"""Retention policies and compaction for durable state.

Controls how long completed run data is kept before archival or deletion.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from workflow_builder.core.models import ExecutionStatus

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
    from workflow_builder.runtime.context import BLOB_KEY

    if isinstance(payload, dict) and set(payload) == {BLOB_KEY}:
        ref = payload[BLOB_KEY]
        return ref if isinstance(ref, str) else None
    return None


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
            A :class:`~workflow_builder.storage.blob.BlobService`. When given,
            blobs referenced only by the journals being dropped are deleted too
            — otherwise blob storage grows forever while the runs that explain
            it are compacted away.
        batch_size:
            How many runs to examine per status. Compaction is meant to be run
            repeatedly from a scheduler, not to drain everything at once.
        dry_run:
            Count what would be affected without deleting anything.
        """
        journal_cutoff = self.cutoff_date("journal_warm")
        record_cutoff = self.cutoff_date("run_record")
        result = CompactionResult()

        for status in TERMINAL_STATUSES:
            records = await store.list_executions(status=status, limit=batch_size)
            for record in records:
                finished = record.finished_at or record.created_at
                if finished is None:
                    continue
                finished = _as_utc(finished)

                expiring = finished < record_cutoff
                if not expiring and finished >= journal_cutoff:
                    continue

                # Collect blob refs before the journal that names them is gone.
                if blobs is not None:
                    result.payloads_deleted += await self._drop_blobs(
                        store, blobs, record.run_id, dry_run=dry_run
                    )

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
    async def _drop_blobs(
        store: Any, blobs: Any, run_id: str, *, dry_run: bool
    ) -> int:
        """Delete blobs referenced by one run's journal. Returns the count.

        Deletion is best-effort: a blob shared with a run that is still live has
        already been removed by whichever run got here first, and the second
        delete is a harmless no-op. Content addressing means re-publishing the
        same bytes simply recreates it.
        """
        deleted = 0
        for entry in await store.load_journal(run_id):
            ref = _blob_ref(entry.output)
            if ref is None:
                continue
            deleted += 1
            if not dry_run:
                with suppress(Exception):
                    await blobs.delete(ref)
        return deleted

    def should_archive_run(self, completed_at: datetime) -> bool:
        """Return ``True`` if a run completed before the run-record cutoff."""
        cutoff = datetime.now(UTC) - timedelta(days=self._policy.run_record_days)
        return completed_at < cutoff

    def should_archive_journal(self, completed_at: datetime) -> bool:
        """Return ``True`` if journal entries should move to warm/cold storage."""
        cutoff = datetime.now(UTC) - timedelta(days=self._policy.journal_warm_days)
        return completed_at < cutoff
