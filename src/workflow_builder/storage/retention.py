"""Retention policies and compaction for durable state.

Controls how long completed run data is kept before archival or deletion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel


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

    The manager provides cutoff calculations and archival predicates today.
    The ``compact`` method is a placeholder that will gain real behaviour once
    the store protocols expose compaction-specific queries.
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

    async def compact(self, store: Any) -> CompactionResult:
        """Run a compaction pass against *store*.

        This is a placeholder — the actual implementation requires store
        methods for scanning and archiving expired data that have not been
        defined yet.  The interface is ready so callers can program against
        it today.
        """
        return CompactionResult()

    def should_archive_run(self, completed_at: datetime) -> bool:
        """Return ``True`` if a run completed before the run-record cutoff."""
        cutoff = datetime.now(UTC) - timedelta(days=self._policy.run_record_days)
        return completed_at < cutoff

    def should_archive_journal(self, completed_at: datetime) -> bool:
        """Return ``True`` if journal entries should move to warm/cold storage."""
        cutoff = datetime.now(UTC) - timedelta(days=self._policy.journal_warm_days)
        return completed_at < cutoff
