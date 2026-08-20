"""A record written by one version of LOOM stays readable by another.

The durability claim is that a journal survives a deploy. Nothing tested it, and
for two entirely ordinary changes it did not hold: adding an enum member made
every journal containing it unloadable by an older version, and adding a
required field made every *existing* row unloadable by the newer one. A third
failure lost data with no error at all — ``extra="ignore"`` plus a whole-record
rewrite silently deleted fields during a mixed-version window.

The golden journal below is the load-bearing part of this file. It is a frozen
literal, not something generated from today's models, because a fixture built by
the code under test agrees with that code by construction and proves nothing.
"""

from __future__ import annotations

import json

import pytest

from loom import Context, Runtime, step, workflow
from loom.core.models import ExecutionRecord, ExecutionStatus, TriggerKind, TriggerRecord
from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry
from loom.stores.memory import MemoryStore

# --------------------------------------------------------------------------
# Reading forward: a record from a NEWER version
# --------------------------------------------------------------------------


class TestUnknownEnumMembers:
    """A member added in a later version must not make a record unreadable."""

    def test_an_unknown_run_status_loads(self) -> None:
        record = ExecutionRecord.model_validate(
            {"run_id": "r1", "workflow": "w", "status": "paused"}
        )

        assert record.status is ExecutionStatus.UNKNOWN

    def test_an_unknown_status_is_not_treated_as_terminal(self) -> None:
        """The consequence that matters more than loading.

        Retention deletes terminal runs. A status nobody can interpret must not
        be guessed into that set — the run may be very much alive.
        """
        record = ExecutionRecord.model_validate(
            {"run_id": "r1", "workflow": "w", "status": "paused"}
        )

        assert not record.status.is_terminal

    def test_an_unknown_entry_kind_loads(self) -> None:
        entry = JournalEntry.model_validate(
            {"path": "0", "kind": "quantum_call", "name": "x"}
        )

        assert entry.kind is EntryKind.UNKNOWN

    def test_an_unknown_entry_status_is_not_settled(self) -> None:
        """Replay serves settled entries instead of running them.

        An outcome this version cannot read is not an outcome, so serving it
        back would hand the workflow a value nobody could interpret.
        """
        entry = JournalEntry.model_validate(
            {"path": "0", "kind": "step", "name": "x", "status": "compensated"}
        )

        assert entry.status is EntryStatus.UNKNOWN
        assert not entry.is_settled

    def test_an_unknown_trigger_kind_loads(self) -> None:
        record = ExecutionRecord.model_validate(
            {"run_id": "r1", "workflow": "w", "trigger": "telepathy"}
        )

        assert record.trigger is TriggerKind.UNKNOWN

    def test_an_unknown_entry_still_renders(self) -> None:
        """`to_record` looked its status up in a dict with `[]`.

        A member added later would have turned "show me this run" into a
        KeyError — the record loads and then cannot be displayed, which is the
        same outage one layer further along.
        """
        entry = JournalEntry.model_validate(
            {"path": "0", "kind": "step", "name": "x", "status": "compensated"}
        )

        assert entry.to_record(0) is not None

    def test_a_typo_in_application_code_still_raises(self) -> None:
        """Tolerance is for *reading stored data*, never for constructing it.

        Putting this on the enum's own `_missing_` would have made
        `ExecutionStatus("runnning")` a silent sentinel — trading one silent
        failure for another.
        """
        with pytest.raises(ValueError):
            ExecutionStatus("runnning")


class TestUnknownFields:
    """A field added in a later version must survive a round trip, not be dropped."""

    def test_an_unknown_field_is_preserved_on_rewrite(self) -> None:
        """The mixed-version data-loss case.

        The stores rewrite the whole record on every write, so `extra="ignore"`
        does not merely skip an unknown field — an old pod reads it, drops it,
        writes the record back without it, and nothing can tell a destroyed
        value from one never set.
        """
        record = ExecutionRecord.model_validate(
            {"run_id": "r1", "workflow": "w", "cost_centre": "eng-42"}
        )

        assert json.loads(record.model_dump_json())["cost_centre"] == "eng-42"

    def test_an_unknown_journal_field_is_preserved(self) -> None:
        entry = JournalEntry.model_validate(
            {"path": "0", "kind": "step", "name": "x", "span_id": "abc"}
        )

        assert json.loads(entry.model_dump_json())["span_id"] == "abc"

    def test_an_unknown_trigger_field_is_preserved(self) -> None:
        record = TriggerRecord.model_validate(
            {"trigger_id": "t1", "workflow": "w", "tenant": "acme"}
        )

        assert json.loads(record.model_dump_json())["tenant"] == "acme"

    async def test_an_unknown_field_survives_a_store_round_trip(self) -> None:
        """End to end, not just at the model boundary."""
        store = MemoryStore()
        await store.create_execution(
            ExecutionRecord.model_validate(
                {"run_id": "r1", "workflow": "w", "cost_centre": "eng-42"}
            )
        )

        loaded = await store.get_execution("r1")

        assert json.loads(loaded.model_dump_json())["cost_centre"] == "eng-42"


# --------------------------------------------------------------------------
# Reading backward: a journal from an OLDER version
# --------------------------------------------------------------------------

#: A journal as an earlier LOOM wrote it. Frozen by hand: no `schema_version`,
#: no `contract_hash`/`closure_hash`, no `idem_key`, and `usage` absent rather
#: than null. Every one of those is a field that has since been added, which is
#: precisely what a reader has to tolerate.
GOLDEN_JOURNAL = [
    {
        "path": "0",
        "kind": "step",
        "name": "fetch",
        "status": "completed",
        # Empty on purpose. This journal stands for one written before argument
        # verification existed, and such an entry carries no fingerprint at all
        # — which is exactly what the verifier skips. The placeholder that used
        # to sit here ("fetch:1") was not a shape any release ever wrote, so it
        # was not testing schema tolerance; it was testing what happens when the
        # arguments genuinely disagree, which is a different question and now has
        # its own test below.
        "fingerprint": "",
        "input": {"args": [2], "kwargs": {}},
        "output": 4,
        "attempts": 1,
    },
    {
        "path": "1",
        "kind": "side_effect",
        "name": "now",
        "status": "completed",
        "output": "2026-01-01T00:00:00+00:00",
        "attempts": 1,
    },
    {
        "path": "2",
        "kind": "step",
        "name": "double",
        "status": "completed",
        "fingerprint": "",
        "input": {"args": [4], "kwargs": {}},
        "output": 8,
        "attempts": 1,
    },
]

GOLDEN_RECORD = {
    "run_id": "run_golden",
    "workflow": "golden",
    "status": "running",
    "trigger": "manual",
    "input": 2,
    "attempt": 1,
}


@step
async def fetch(n: int) -> int:
    return n * 2


@step
async def double(n: int) -> int:
    return n * 2


@workflow(name="golden")
async def golden(ctx: Context, n: int) -> int:
    first = await ctx.step(fetch, n)
    ctx.now()  # journals as a side_effect, so the golden paths line up
    return await ctx.step(double, first)


class TestAnOlderJournalStillReplays:
    """The guarantee the whole subsystem rests on, and had no test for."""

    def test_every_golden_entry_loads(self) -> None:
        entries = [JournalEntry.model_validate(e) for e in GOLDEN_JOURNAL]

        assert [e.name for e in entries] == ["fetch", "now", "double"]
        assert all(e.status is EntryStatus.COMPLETED for e in entries)

    async def test_a_journal_with_no_fingerprints_replays_under_the_strict_default(
        self,
    ) -> None:
        """The upgrade guarantee for `VerifyMode.STRICT` becoming the default.

        An entry written before argument verification existed carries no
        fingerprint, and refusing those would strand every in-flight run on
        upgrade — a far worse failure than the one strict verification prevents.
        """
        store = MemoryStore()
        await store.create_execution(ExecutionRecord.model_validate(GOLDEN_RECORD))
        await store.save_journal(
            "run_golden", [JournalEntry.model_validate(e) for e in GOLDEN_JOURNAL]
        )

        rt = Runtime(store=store)  # strict verification, by default
        rt.register(golden)
        try:
            result = await rt.replay("run_golden")
        finally:
            await rt.shutdown()

        assert result.status is ExecutionStatus.COMPLETED

    async def test_arguments_that_genuinely_disagree_are_refused(self) -> None:
        """The question the old placeholder fingerprint was really asking.

        A recorded fingerprint that does not match the replaying call means the
        stored value may belong to a different call site, and serving it is how
        a replay silently produces another call's answer.
        """
        divergent = [dict(entry) for entry in GOLDEN_JOURNAL]
        divergent[0]["fingerprint"] = "a-fingerprint-from-another-call"

        store = MemoryStore()
        await store.create_execution(ExecutionRecord.model_validate(GOLDEN_RECORD))
        await store.save_journal(
            "run_golden", [JournalEntry.model_validate(e) for e in divergent]
        )

        rt = Runtime(store=store)
        rt.register(golden)
        try:
            result = await rt.replay("run_golden")
        finally:
            await rt.shutdown()

        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "different arguments" in result.error.message

    def test_fields_added_since_take_their_defaults(self) -> None:
        entry = JournalEntry.model_validate(GOLDEN_JOURNAL[0])

        assert entry.schema_version == 1
        assert entry.contract_hash == ""
        assert entry.usage is None

    async def test_a_journal_from_an_older_version_replays(self) -> None:
        """Loaded, re-entered, and served from — not merely parsed.

        A record that validates and then cannot drive a replay would satisfy
        every other test in this file and still lose every in-flight run.
        """
        store = MemoryStore()
        await store.create_execution(ExecutionRecord.model_validate(GOLDEN_RECORD))
        await store.save_journal(
            "run_golden", [JournalEntry.model_validate(e) for e in GOLDEN_JOURNAL]
        )

        rt = Runtime(store=store)
        rt.register(golden)
        try:
            result = await rt.replay("run_golden")
        finally:
            await rt.shutdown()

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == 8

    async def test_the_replay_serves_recorded_values_rather_than_re_running(self) -> None:
        """`fetch` recorded 4 for input 2; the live step would also return 4.

        So the entry is seeded with a value the code could not produce, and the
        output proves the journal was read rather than the body re-executed.
        """
        store = MemoryStore()
        await store.create_execution(ExecutionRecord.model_validate(GOLDEN_RECORD))
        doctored = [dict(e) for e in GOLDEN_JOURNAL]
        doctored[0]["output"] = 100
        doctored[2]["input"] = {"args": [100], "kwargs": {}}
        doctored[2]["output"] = 200
        await store.save_journal(
            "run_golden", [JournalEntry.model_validate(e) for e in doctored]
        )

        rt = Runtime(store=store)
        rt.register(golden)
        try:
            result = await rt.replay("run_golden")
        finally:
            await rt.shutdown()

        assert result.output == 200


class TestNoNewRequiredFields:
    """A required field added to a persisted model breaks every existing row.

    Stated as a test rather than a convention, because a convention nobody can
    run is one the next contributor has no way to discover.
    """

    @pytest.mark.parametrize(
        "model",
        [ExecutionRecord, JournalEntry, TriggerRecord],
        ids=lambda m: m.__name__,
    )
    def test_only_the_documented_fields_are_required(self, model) -> None:
        required = {
            name for name, f in model.model_fields.items() if f.is_required()
        }
        allowed = {
            "ExecutionRecord": {"workflow"},
            "JournalEntry": {"path", "kind", "name"},
            "TriggerRecord": {"workflow"},
        }[model.__name__]

        assert required == allowed, (
            f"{model.__name__} gained a required field: {sorted(required - allowed)}. "
            "Every row written before it ships now fails validation, which for "
            "JournalEntry means every in-flight run becomes unrecoverable. Give "
            "it a default instead."
        )
