"""Compacting one run must not delete a blob another run still needs.

Blobs are content-addressed, so two runs producing byte-identical output share
one — and for a deterministic step that is the *normal* case, not a coincidence.
Retention deleted by scanning one run's journal and dropping every ref it named,
so compacting run A destroyed a payload run B was still going to replay. It
surfaced as a bare 64-character hex string out of `BlobService.load`, and it was
the one data-loss path that reached a *correctly configured* deployment.

The index lives in `CacheStore`, which every backend already implements, so
there is no schema change and a host's own store gets it for free.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, step, workflow
from loom.blobs.blob import BlobService, blob_backend_from_url
from loom.blobs.refcount import BLOB_REF_PREFIX, record_refs, referents, release_ref
from loom.blobs.retention import RetentionManager, RetentionPolicy
from loom.stores.memory import MemoryStore

BIG = "x" * 400_000  # over the 256 KiB offload threshold


@step
async def produce(seed: int) -> str:
    # Deliberately ignores its input: two runs produce identical bytes, which
    # content addressing then collapses to one blob. That is the sharing.
    return BIG


@workflow(name="sharer")
async def sharer(ctx: Context, seed: int) -> int:
    return len(await ctx.step(produce, seed))


@pytest.fixture
async def wired(tmp_path):
    blobs = BlobService(blob_backend_from_url(f"file://{tmp_path}"))
    store = MemoryStore()
    rt = Runtime(store=store, blobs=blobs)
    rt.register(sharer)
    try:
        yield rt, store, blobs
    finally:
        await rt.shutdown()


async def _age(store, run_id, days: float) -> None:
    from datetime import UTC, datetime, timedelta

    record = await store.get_execution(run_id)
    when = datetime.now(UTC) - timedelta(days=days)
    record.created_at = when
    record.finished_at = when
    await store.update_execution(record)


class TestSharedBlobsSurvive:
    async def test_two_runs_share_one_blob(self, wired) -> None:
        """The premise. Without sharing there is nothing to protect."""
        rt, store, _ = wired
        a = await rt.run(sharer, 1)
        b = await rt.run(sharer, 2)

        from loom.blobs.refcount import refs_in

        refs_a = refs_in(await store.load_journal(a.run_id))
        refs_b = refs_in(await store.load_journal(b.run_id))

        assert refs_a and refs_a == refs_b, "identical bytes must resolve to one blob"

    async def test_compacting_one_leaves_the_other_readable(self, wired) -> None:
        """The bug, as a test. This deleted B's payload."""
        rt, store, blobs = wired
        a = await rt.run(sharer, 1)
        b = await rt.run(sharer, 2)
        await _age(store, a.run_id, 400)  # old enough to delete outright

        result = await RetentionManager(RetentionPolicy()).compact(store, blobs=blobs)

        assert result.runs_archived == 1
        assert result.payloads_deleted == 0, "B still references that blob"
        from loom.blobs.refcount import refs_in

        (ref,) = refs_in(await store.load_journal(b.run_id))
        assert await blobs.load(ref)

    async def test_the_surviving_run_still_replays(self, wired) -> None:
        """Readable is not the claim — replayable is.

        A payload that loads but cannot drive a replay would satisfy the test
        above and still lose the run.
        """
        rt, store, blobs = wired
        a = await rt.run(sharer, 1)
        b = await rt.run(sharer, 2)
        await _age(store, a.run_id, 400)

        await RetentionManager(RetentionPolicy()).compact(store, blobs=blobs)
        replayed = await rt.replay(b.run_id)

        assert replayed.output == len(BIG)

    async def test_compacting_the_last_referent_does_delete(self, wired) -> None:
        """The negative control — otherwise the fix is just "never delete"."""
        rt, store, blobs = wired
        a = await rt.run(sharer, 1)
        b = await rt.run(sharer, 2)
        await _age(store, a.run_id, 400)
        await _age(store, b.run_id, 400)

        result = await RetentionManager(RetentionPolicy()).compact(store, blobs=blobs)

        assert result.runs_archived == 2
        assert result.payloads_deleted == 1
        from loom.blobs.refcount import refs_in  # noqa: F401

        assert result.payloads_deleted == 1

    async def test_a_replay_clone_is_recorded_as_a_referent(self, wired) -> None:
        """`replay()` copies the journal, so the clone shares every ref.

        It bypasses `persist_journal`, which is where indexing happens — so the
        copy has to record the clone itself or the sharing stays invisible.
        """
        rt, store, _ = wired
        original = await rt.run(sharer, 1)
        await rt.replay(original.run_id)

        from loom.blobs.refcount import refs_in

        (ref,) = refs_in(await store.load_journal(original.run_id))
        holders = await referents(store, ref)

        assert holders is not None
        assert f"{original.run_id}:replay" in holders

    async def test_a_dry_run_does_not_disturb_the_index(self, wired) -> None:
        """A dry run that decremented would make the next real pass over-delete."""
        rt, store, blobs = wired
        a = await rt.run(sharer, 1)
        await rt.run(sharer, 2)
        await _age(store, a.run_id, 400)

        before = await referents(store, next(iter(_refs(await store.load_journal(a.run_id)))))
        await RetentionManager(RetentionPolicy()).compact(store, blobs=blobs, dry_run=True)
        after = await referents(store, next(iter(_refs(await store.load_journal(a.run_id)))))

        assert before == after


def _refs(entries):
    from loom.blobs.refcount import refs_in

    return refs_in(entries)


class TestTheIndexItself:
    async def test_an_unknown_ref_is_none_not_empty(self) -> None:
        """`None` and `[]` are different answers and callers branch on it.

        `None` means "written before the index existed", `[]` means "genuinely
        nobody" — reading the first as the second is what would licence a delete.
        """
        assert await referents(MemoryStore(), "blob:deadbeef") is None

    async def test_releasing_an_unknown_ref_reports_unknown(self) -> None:
        assert await release_ref(MemoryStore(), "blob:deadbeef", "run-1") is None

    async def test_the_last_referent_reports_sole_ownership(self) -> None:
        from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry

        store = MemoryStore()
        entry = JournalEntry(
            path="0",
            kind=EntryKind.STEP,
            name="s",
            status=EntryStatus.COMPLETED,
            output={"__blob__": "blob:abc"},
        )
        await record_refs(store, "run-1", [entry])
        await record_refs(store, "run-2", [entry])

        assert await release_ref(store, "blob:abc", "run-1") is False
        assert await release_ref(store, "blob:abc", "run-2") is True
        assert await store.get(BLOB_REF_PREFIX + "blob:abc") is None

    async def test_recording_is_idempotent(self) -> None:
        """A retried flush re-records the same entries; the run must appear once
        or a single release would leave a phantom referent behind forever."""
        from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry

        store = MemoryStore()
        entry = JournalEntry(
            path="0",
            kind=EntryKind.STEP,
            name="s",
            status=EntryStatus.COMPLETED,
            output={"__blob__": "blob:abc"},
        )
        await record_refs(store, "run-1", [entry])
        await record_refs(store, "run-1", [entry])

        assert await referents(store, "blob:abc") == ["run-1"]

    async def test_entries_with_no_blobs_cost_nothing(self) -> None:
        """The common case must not pay for the rare one."""
        from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry

        calls = 0

        class Counting(MemoryStore):
            async def get(self, key: str):
                nonlocal calls
                calls += 1
                return await super().get(key)

        plain = JournalEntry(
            path="0", kind=EntryKind.STEP, name="s",
            status=EntryStatus.COMPLETED, output={"value": 1},
        )
        await record_refs(Counting(), "run-1", [plain])

        assert calls == 0
