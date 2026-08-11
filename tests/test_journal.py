"""Tests for journal hashes, idempotency keys, and hash mismatch detection."""

from __future__ import annotations

import pytest

from workflow_builder.core.exceptions import NondeterminismError
from workflow_builder.runtime.journal import (
    CompatibilityMode,
    EntryKind,
    EntryStatus,
    Journal,
    JournalEntry,
    Scope,
    path_order,
)

# ---------------------------------------------------------------------------
# JournalEntry hash fields
# ---------------------------------------------------------------------------


class TestJournalEntryHashes:
    def test_hash_fields_default_empty(self) -> None:
        entry = JournalEntry(path="0", kind=EntryKind.STEP, name="test")
        assert entry.contract_hash == ""
        assert entry.closure_hash == ""
        assert entry.idem_key == ""

    def test_hash_fields_round_trip(self) -> None:
        entry = JournalEntry(
            path="0",
            kind=EntryKind.STEP,
            name="test",
            contract_hash="abc123",
            closure_hash="def456",
            idem_key="order:42",
        )
        data = entry.model_dump()
        restored = JournalEntry.model_validate(data)
        assert restored.contract_hash == "abc123"
        assert restored.closure_hash == "def456"
        assert restored.idem_key == "order:42"

    def test_json_round_trip(self) -> None:
        entry = JournalEntry(
            path="0",
            kind=EntryKind.STEP,
            name="test",
            contract_hash="aaa",
            closure_hash="bbb",
            idem_key="key1",
            status=EntryStatus.COMPLETED,
            output=42,
        )
        json_str = entry.model_dump_json()
        restored = JournalEntry.model_validate_json(json_str)
        assert restored.contract_hash == "aaa"
        assert restored.closure_hash == "bbb"
        assert restored.idem_key == "key1"
        assert restored.output == 42


# ---------------------------------------------------------------------------
# Journal.lookup with hash verification
# ---------------------------------------------------------------------------


class TestJournalLookupHashes:
    def test_matching_hashes_replay(self) -> None:
        j = Journal()
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="fetch",
                status=EntryStatus.COMPLETED,
                contract_hash="aaa",
                closure_hash="bbb",
                output="ok",
            )
        )
        result = j.lookup("0", EntryKind.STEP, "fetch", contract_hash="aaa", closure_hash="bbb")
        assert result is not None
        assert result.output == "ok"

    def test_contract_mismatch_strict_raises(self) -> None:
        j = Journal(compatibility=CompatibilityMode.STRICT)
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="fetch",
                status=EntryStatus.COMPLETED,
                contract_hash="aaa",
                closure_hash="bbb",
                output="ok",
            )
        )
        with pytest.raises(NondeterminismError, match="contract hash changed"):
            j.lookup("0", EntryKind.STEP, "fetch", contract_hash="CHANGED", closure_hash="bbb")

    def test_closure_mismatch_strict_raises(self) -> None:
        j = Journal(compatibility=CompatibilityMode.STRICT)
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="fetch",
                status=EntryStatus.COMPLETED,
                contract_hash="aaa",
                closure_hash="bbb",
                output="ok",
            )
        )
        with pytest.raises(NondeterminismError, match="closure hash changed"):
            j.lookup("0", EntryKind.STEP, "fetch", contract_hash="aaa", closure_hash="CHANGED")

    def test_closure_mismatch_resume_truncates(self) -> None:
        j = Journal(compatibility=CompatibilityMode.RESUME_FROM_DIVERGENCE)
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="fetch",
                status=EntryStatus.COMPLETED,
                contract_hash="aaa",
                closure_hash="bbb",
                output="ok",
            )
        )
        result = j.lookup("0", EntryKind.STEP, "fetch", contract_hash="aaa", closure_hash="CHANGED")
        assert result is None
        assert len(j) == 0  # truncated

    def test_empty_stored_hashes_no_mismatch(self) -> None:
        """Backward compat: if journal entry has no hashes, no mismatch is raised."""
        j = Journal()
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="fetch",
                status=EntryStatus.COMPLETED,
                output="ok",
            )
        )
        result = j.lookup(
            "0", EntryKind.STEP, "fetch", contract_hash="anything", closure_hash="anything"
        )
        assert result is not None

    def test_empty_supplied_hashes_no_mismatch(self) -> None:
        """If caller doesn't supply hashes, existing hashes are ignored."""
        j = Journal()
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="fetch",
                status=EntryStatus.COMPLETED,
                contract_hash="aaa",
                closure_hash="bbb",
                output="ok",
            )
        )
        result = j.lookup("0", EntryKind.STEP, "fetch")
        assert result is not None


# ---------------------------------------------------------------------------
# Existing journal behavior (regression tests)
# ---------------------------------------------------------------------------


class TestJournalBasics:
    def test_scope_allocates_sequential_paths(self) -> None:
        scope = Scope()
        assert scope.allocate() == "0"
        assert scope.allocate() == "1"
        assert scope.allocate() == "2"

    def test_nested_scope(self) -> None:
        scope = Scope()
        scope.allocate()  # "0"
        path = scope.allocate()  # "1"
        child = scope.child(path)
        assert child.allocate() == "1.0"
        assert child.allocate() == "1.1"

    def test_path_order(self) -> None:
        paths = ["2", "10", "1", "3.1", "3.0"]
        sorted_paths = sorted(paths, key=path_order)
        assert sorted_paths == ["1", "2", "3.0", "3.1", "10"]

    def test_put_and_get(self) -> None:
        j = Journal()
        entry = JournalEntry(
            path="0", kind=EntryKind.STEP, name="test", status=EntryStatus.COMPLETED, output=42
        )
        j.put(entry)
        assert j.get("0") is entry
        assert j.get("1") is None

    def test_lookup_kind_mismatch_strict(self) -> None:
        j = Journal(compatibility=CompatibilityMode.STRICT)
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="test",
                status=EntryStatus.COMPLETED,
                output=1,
            )
        )
        with pytest.raises(NondeterminismError, match="replay diverged"):
            j.lookup("0", EntryKind.SLEEP, "test")

    def test_lookup_name_mismatch_resume(self) -> None:
        j = Journal(compatibility=CompatibilityMode.RESUME_FROM_DIVERGENCE)
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="old_name",
                status=EntryStatus.COMPLETED,
                output=1,
            )
        )
        result = j.lookup("0", EntryKind.STEP, "new_name")
        assert result is None

    def test_drain_dirty(self) -> None:
        j = Journal()
        e1 = JournalEntry(path="0", kind=EntryKind.STEP, name="a", status=EntryStatus.COMPLETED)
        e2 = JournalEntry(path="1", kind=EntryKind.STEP, name="b", status=EntryStatus.COMPLETED)
        j.put(e1)
        j.put(e2)
        dirty = j.drain_dirty()
        assert len(dirty) == 2
        assert j.drain_dirty() == []  # second drain is empty

    def test_truncate(self) -> None:
        j = Journal()
        for i in range(5):
            j.put(
                JournalEntry(
                    path=str(i),
                    kind=EntryKind.STEP,
                    name=f"step_{i}",
                    status=EntryStatus.COMPLETED,
                )
            )
        j.truncate("2")
        assert len(j) == 2  # only "0" and "1" remain

    def test_replayed_counter(self) -> None:
        j = Journal()
        j.put(
            JournalEntry(
                path="0",
                kind=EntryKind.STEP,
                name="a",
                status=EntryStatus.COMPLETED,
                output=1,
            )
        )
        assert j.replayed == 0
        j.lookup("0", EntryKind.STEP, "a")
        assert j.replayed == 1

    def test_entry_depth(self) -> None:
        assert JournalEntry(path="0", kind=EntryKind.STEP, name="a").depth == 0
        assert JournalEntry(path="1.2", kind=EntryKind.STEP, name="a").depth == 1
        assert JournalEntry(path="1.2.3", kind=EntryKind.STEP, name="a").depth == 2
