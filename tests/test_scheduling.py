"""Scheduling: what fires, exactly once, at the moment the schedule named.

`test_dispatcher.py` covers the store surface and the happy path — a due
trigger fires. This file covers the properties that only appear when a
scheduler meets reality: a second replica, a clock that is late, a process that
dies mid-tick, and a downtime window that has to be accounted for one way or
the other.

Every test here uses a `ManualClock` and an explicit moment, because a
scheduling test that reads the wall clock is a scheduling test that fails at
midnight, at a DST boundary, or on a loaded CI box.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from loom import Context, workflow
from loom.core.models import TriggerRecord
from loom.runtime.clock import ManualClock
from loom.runtime.dispatcher import (
    Fire,
    TriggerDispatcher,
    _is_ready,
    _occurrences_due,
    _trigger_id,
)
from loom.runtime.engine import Runtime
from loom.stores.sqlite import SQLiteStore
from loom.triggers.specs import Interval, Schedule

NINE_AM = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@workflow(name="hourly_report", triggers=[Schedule("0 * * * *")])
async def hourly_report(ctx: Context, _: None = None) -> str:
    return "reported"


@workflow(name="every_hour", triggers=[Interval(3600)])
async def every_hour(ctx: Context, _: None = None) -> str:
    return "ticked"


def a_runtime(db: str, clock: ManualClock) -> Runtime:
    """A Runtime over a *shared* store — the whole point of these tests.

    Two Runtimes over one SQLite file is what two replicas are, minus the
    network. Anything that needs coordinating has to coordinate through the
    store, because that is the only thing they share.
    """
    rt = Runtime(store=SQLiteStore(db), clock=clock)
    rt.register(hourly_report)
    return rt


class TestOneOccurrenceIsOneRun:
    """The property a scheduler exists to provide.

    A cron that fires twice is not a slow scheduler, it is a wrong one: the
    second run bills the customer again, sends the digest again, or reconciles
    a ledger against itself. It is the failure this whole file is about.
    """

    async def test_two_dispatchers_over_one_store_fire_once(
        self, tmp_path
    ) -> None:
        """Two replicas, one due occurrence.

        This is the shape of every deploy that briefly runs two pods, every
        blue/green cutover, and every operator who started a second worker to
        drain a backlog.
        """
        db = str(tmp_path / "sched.db")
        clock = ManualClock(NINE_AM)

        first = a_runtime(db, clock)
        second = a_runtime(db, clock)

        # Registered once — they are the same deployment, sharing a store.
        await TriggerDispatcher(first).register(hourly_report)

        # Concurrently, which is the only version of this that is a race.
        # Run them one after the other and the first has already advanced
        # next_fire_at before the second looks — the bug hides behind the very
        # sequencing that production does not give you.
        at_ten = NINE_AM + timedelta(hours=1)
        await asyncio.gather(
            TriggerDispatcher(first).tick(at_ten),
            TriggerDispatcher(second).tick(at_ten),
        )

        runs = await first.store.list_executions(workflow="hourly_report")
        assert len(runs) == 1, (
            f"the 10:00 occurrence produced {len(runs)} runs; a cron that "
            "fires twice is not a slow scheduler, it is a wrong one"
        )

    async def test_a_dispatcher_that_dies_before_advancing_fires_once(
        self, tmp_path
    ) -> None:
        """The crash window.

        `tick()` submits and *then* advances `next_fire_at`. A process killed
        between those two writes leaves a trigger that is still due, so the
        next dispatcher to look fires the same occurrence again. Restarting a
        worker must not re-run the hour it already ran.
        """
        db = str(tmp_path / "crash.db")
        clock = ManualClock(NINE_AM)
        rt = a_runtime(db, clock)
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(hourly_report)

        at_ten = NINE_AM + timedelta(hours=1)

        # Fire, then throw away the advancement — exactly what a SIGKILL
        # between the two writes leaves behind.
        triggers = await rt.store.list_triggers()
        before = triggers[0].next_fire_at
        await dispatcher.tick(at_ten)
        await rt.store.save_trigger(
            triggers[0].model_copy(update={"next_fire_at": before})
        )

        await TriggerDispatcher(a_runtime(db, clock)).tick(at_ten)

        runs = await rt.store.list_executions(workflow="hourly_report")
        assert len(runs) == 1, (
            f"the restart re-ran the 10:00 occurrence ({len(runs)} runs)"
        )


class TestRegistrationSurvivesRestarts:
    """What a process start does to a schedule that already exists.

    Registration runs on every boot — it is how a workflow's triggers get into
    the store in the first place. So it has to be idempotent, and the failure
    when it is not is silent and compounding: nothing errors, the schedule just
    quietly does more than it was asked to, a bit more after every deploy.
    """

    async def test_restarting_does_not_duplicate_the_trigger(
        self, tmp_path
    ) -> None:
        db = str(tmp_path / "boots.db")
        clock = ManualClock(NINE_AM)

        for _ in range(3):
            await TriggerDispatcher(a_runtime(db, clock)).register(hourly_report)

        rt = a_runtime(db, clock)
        triggers = await rt.store.list_triggers()
        assert len(triggers) == 1, (
            f"three boots left {len(triggers)} trigger records for one "
            "declared schedule"
        )

    async def test_restarting_does_not_multiply_the_runs(self, tmp_path) -> None:
        """The consequence, stated in the unit that matters.

        Duplicate records each fire once, and each carries a *different*
        trigger id — so the occurrence key cannot collapse them. Three deploys
        means the 10:00 report is sent three times.
        """
        db = str(tmp_path / "boot_runs.db")
        clock = ManualClock(NINE_AM)

        for _ in range(3):
            await TriggerDispatcher(a_runtime(db, clock)).register(hourly_report)

        rt = a_runtime(db, clock)
        await TriggerDispatcher(rt).tick(NINE_AM + timedelta(hours=1))

        runs = await rt.store.list_executions(workflow="hourly_report")
        assert len(runs) == 1, (
            f"the 10:00 occurrence ran {len(runs)} times after three restarts"
        )

    async def test_a_restart_does_not_swallow_a_pending_occurrence(
        self, tmp_path
    ) -> None:
        """A restart must not consume the fire it was late for.

        Registered at 09:00, the 10:00 occurrence comes due. If the pod
        restarts at 10:30 *before* anything ticked, and registration recomputes
        `next_fire_at` from the current moment, that occurrence is silently
        replaced by 11:00 and never runs. A deployment restarting more often
        than its schedule fires would then never fire at all — looking like a
        broken cron rather than like the restarts.
        """
        db = str(tmp_path / "reset.db")
        await TriggerDispatcher(a_runtime(db, ManualClock(NINE_AM))).register(
            hourly_report
        )

        # The pod restarts at 10:30, having never ticked at 10:00.
        half_past = ManualClock(NINE_AM + timedelta(hours=1, minutes=30))
        rt = a_runtime(db, half_past)
        await TriggerDispatcher(rt).register(hourly_report)

        pending = (await rt.store.list_triggers())[0]
        assert pending.next_fire_at == NINE_AM + timedelta(hours=1), (
            f"the restart moved the pending occurrence to "
            f"{pending.next_fire_at}; 10:00 will never run"
        )

        fired = await TriggerDispatcher(rt).tick(half_past.now())
        assert len(fired) == 1, "the occurrence the restart was late for is gone"

    async def test_a_changed_schedule_replaces_the_old_one(
        self, tmp_path
    ) -> None:
        """Triggers are declared in code, so code is the truth.

        Changing a cron from hourly to daily must not leave the hourly one
        firing forever — an orphan nobody can see in the source and nobody
        thinks to look for in the store.
        """
        db = str(tmp_path / "changed.db")
        clock = ManualClock(NINE_AM)

        @workflow(name="reschedulable", triggers=[Schedule("0 * * * *")])
        async def hourly(ctx: Context, _: None = None) -> str:
            return "hourly"

        @workflow(name="reschedulable", triggers=[Schedule("0 3 * * *")])
        async def daily(ctx: Context, _: None = None) -> str:
            return "daily"

        # Separate Runtimes, because a schedule change is a redeploy: the new
        # process never sees the old definition, it only sees the store.
        before = Runtime(store=SQLiteStore(db), clock=clock)
        await TriggerDispatcher(before).register(hourly)

        rt = Runtime(store=SQLiteStore(db), clock=clock)
        await TriggerDispatcher(rt).register(daily)

        triggers = await rt.store.list_triggers(workflow="reschedulable")
        assert len(triggers) == 1, (
            f"the old schedule outlived the change ({len(triggers)} records)"
        )
        assert triggers[0].spec.get("cron") == "0 3 * * *"


class TestTheTriggerIdIsAContract:
    """The id is derived, so the derivation is a migration surface.

    Every stored trigger is addressed by it, so changing how it is computed
    orphans every trigger already in a production store: the new process finds
    nothing under the new id, registers a fresh record, and the old one is
    retired — losing `last_fire_at`, `run_count`, and any pending occurrence.
    Nothing errors. Pinning the literals means that change cannot be made by
    accident, only deliberately and with a migration.
    """

    def test_a_cron_trigger_id_is_stable(self) -> None:
        assert (
            _trigger_id("billing", Schedule("0 9 * * *")) == "trg_fc560d224e2dfacd"
        )

    def test_an_interval_trigger_id_is_stable(self) -> None:
        assert _trigger_id("billing", Interval(3600)) == "trg_0712b3cc0d175d08"

    def test_the_id_does_not_depend_on_when_it_was_computed(self) -> None:
        """The bug this catches is subtle and total.

        `Schedule.describe()` includes `next_fire`, a timestamp evaluated when
        it is called. Hashing the whole description therefore made a trigger's
        id depend on what time the process booted — so two deployments minutes
        apart produced two ids for one declared schedule, which is exactly the
        duplication the id was introduced to prevent.
        """
        spec = Schedule("0 9 * * *")
        first = _trigger_id("billing", spec)
        later = _trigger_id("billing", Schedule("0 9 * * *"))

        assert first == later
        assert "next_fire" in spec.describe(), (
            "describe() no longer carries a timestamp; if the allowlist in "
            "_trigger_id was relaxed on that basis, re-check this property"
        )

    def test_policy_is_not_part_of_the_identity(self) -> None:
        """Changing catch-up or jitter must not orphan the trigger.

        A new id means a new record, and the old one is retired along with its
        `last_fire_at`, `run_count`, and any occurrence it still owed. Turning
        on catch-up is not a reason to lose the schedule's history.
        """
        assert _trigger_id("billing", Schedule("0 9 * * *")) == _trigger_id(
            "billing", Schedule("0 9 * * *", catch_up=True, jitter=30)
        )

    def test_the_timezone_is_part_of_the_identity(self) -> None:
        """Same expression, different zone, different schedule — so a change of
        zone must replace the trigger rather than silently keep the old one."""
        assert _trigger_id("billing", Schedule("0 9 * * *")) != _trigger_id(
            "billing", Schedule("0 9 * * *", timezone="Europe/London")
        )

    def test_the_workflow_is_part_of_the_identity(self) -> None:
        assert _trigger_id("billing", Schedule("0 9 * * *")) != _trigger_id(
            "payroll", Schedule("0 9 * * *")
        )


class TestRetiringIsNarrow:
    async def test_another_workflows_trigger_is_left_alone(self, tmp_path) -> None:
        """Reconciling one workflow must not touch another's schedule."""
        db = str(tmp_path / "narrow.db")
        clock = ManualClock(NINE_AM)

        rt = Runtime(store=SQLiteStore(db), clock=clock)
        await TriggerDispatcher(rt).register(hourly_report)
        await TriggerDispatcher(rt).register(every_hour)

        assert len(await rt.store.list_triggers()) == 2

        # Re-registering one of them reconciles only its own.
        await TriggerDispatcher(rt).register(hourly_report)

        assert len(await rt.store.list_triggers()) == 2


class TestTheScheduleIsAGrid:
    """Occurrences belong to the schedule, not to whoever noticed them."""

    async def test_a_cron_noticed_late_stays_on_its_grid(
        self, tmp_path
    ) -> None:
        """An hourly cron noticed at 10:04:37 still next fires at 11:00."""
        db = str(tmp_path / "drift.db")
        clock = ManualClock(NINE_AM)
        rt = a_runtime(db, clock)
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(hourly_report)

        await dispatcher.tick(NINE_AM + timedelta(hours=1, minutes=4, seconds=37))

        triggers = await rt.store.list_triggers()
        assert triggers[0].next_fire_at == NINE_AM + timedelta(hours=2)

    async def test_an_interval_noticed_late_does_not_drag_the_schedule(
        self, tmp_path
    ) -> None:
        """Where advancing from the wall clock actually bites.

        `Interval.next_fire` is `after + every`, and the dispatcher passes
        `now`. So every late tick pushes the next one out by however late this
        one was, and the error accumulates: an hourly interval noticed four
        minutes late becomes hourly-plus-four-minutes, then plus eight. After a
        day the job has silently lost a cycle, and nothing reports it.
        """
        db = str(tmp_path / "interval.db")
        clock = ManualClock(NINE_AM)
        rt = Runtime(store=SQLiteStore(db), clock=clock)
        rt.register(every_hour)
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(every_hour)

        await dispatcher.tick(NINE_AM + timedelta(hours=1, minutes=4, seconds=37))

        triggers = await rt.store.list_triggers()
        assert triggers[0].next_fire_at == NINE_AM + timedelta(hours=2), (
            f"next fire is {triggers[0].next_fire_at}, not 11:00 — the "
            "interval drifted by however late the dispatcher was"
        )


class TestMissedFires:
    """What an outage does to a schedule, decided rather than defaulted.

    `_occurrences_due` is pure, so these state the policy directly against a
    record and a moment — no store, no runtime, no clock to nudge.
    """

    def _record(self, **spec_overrides) -> TriggerRecord:
        spec = Schedule("0 * * * *", **spec_overrides).describe()
        return TriggerRecord(
            trigger_id="trg_test",
            workflow="hourly_report",
            spec=spec,
            next_fire_at=NINE_AM,
        )

    def test_nothing_is_due_before_the_scheduled_moment(self) -> None:
        fires, dropped, nxt = _occurrences_due(
            self._record(), NINE_AM - timedelta(minutes=1)
        )
        assert (fires, dropped) == ([], 0)
        assert nxt == NINE_AM

    def test_skipping_is_the_default_and_fires_once(self) -> None:
        """Four hours of downtime, default policy: the pending occurrence runs
        and the rest are skipped — the behaviour that shipped, now deliberate."""
        fires, dropped, nxt = _occurrences_due(
            self._record(), NINE_AM + timedelta(hours=4)
        )

        assert [f.scheduled_for for f in fires] == [NINE_AM]
        assert nxt == NINE_AM + timedelta(hours=5)

    def test_catch_up_replays_every_missed_occurrence(self) -> None:
        fires, dropped, nxt = _occurrences_due(
            self._record(catch_up=True), NINE_AM + timedelta(hours=4)
        )

        assert [f.scheduled_for for f in fires] == [
            NINE_AM + timedelta(hours=n) for n in range(5)
        ]
        assert dropped == 0
        assert nxt == NINE_AM + timedelta(hours=5)

    def test_each_replayed_occurrence_has_its_own_key(self) -> None:
        """So a backfill interrupted halfway resumes without repeating."""
        fires, _, _ = _occurrences_due(
            self._record(catch_up=True), NINE_AM + timedelta(hours=4)
        )

        assert len({f.key for f in fires}) == len(fires)

    def test_the_ceiling_keeps_the_newest_and_reports_the_rest(self) -> None:
        """Ten days into an outage, the last two days beat the first two."""
        fires, dropped, _ = _occurrences_due(
            self._record(catch_up=True, max_catch_up=2),
            NINE_AM + timedelta(hours=9),
        )

        assert len(fires) == 2
        assert dropped > 0
        assert fires[-1].scheduled_for > fires[0].scheduled_for

    def test_a_long_outage_does_not_walk_the_whole_backlog(self) -> None:
        """A per-minute cron down for a week is ten thousand occurrences.

        Enumerating them all just to discard all but ten would make recovery
        itself the incident, so the walk stops one past the ceiling.
        """
        spec = Schedule("* * * * *", catch_up=True, max_catch_up=5).describe()
        record = TriggerRecord(
            trigger_id="trg_minute",
            workflow="hourly_report",
            spec=spec,
            next_fire_at=NINE_AM,
        )

        fires, dropped, _ = _occurrences_due(record, NINE_AM + timedelta(days=7))

        assert len(fires) == 5
        assert dropped >= 1


class TestJitter:
    """Spreading a thundering herd without touching the schedule."""

    def _record(self, jitter: float) -> TriggerRecord:
        return TriggerRecord(
            trigger_id="trg_jitter",
            workflow="hourly_report",
            spec=Schedule("0 * * * *", jitter=jitter).describe(),
            next_fire_at=NINE_AM,
        )

    def test_no_jitter_is_ready_immediately(self) -> None:
        record = self._record(0)
        fire = Fire("trg_jitter", "hourly_report", NINE_AM)

        assert _is_ready(fire, record, NINE_AM)

    def test_jitter_holds_an_occurrence_back_then_releases_it(self) -> None:
        record = self._record(60)
        fire = Fire("trg_jitter", "hourly_report", NINE_AM)

        held = _is_ready(fire, record, NINE_AM)
        released = _is_ready(fire, record, NINE_AM + timedelta(seconds=60))

        assert not held, "a 60s jitter released the occurrence instantly"
        assert released, "the occurrence never became ready"

    def test_the_delay_is_the_same_in_every_process(self) -> None:
        """Derived, not sampled.

        Two dispatchers must agree on when an occurrence becomes eligible. A
        random draw would let whichever process drew the shortest delay fire
        first, making the spread depend on how many replicas happen to be up.
        """
        record = self._record(3600)
        fire = Fire("trg_jitter", "hourly_report", NINE_AM)
        moment = NINE_AM + timedelta(minutes=17)

        answers = {_is_ready(fire, record, moment) for _ in range(50)}

        assert len(answers) == 1

    def test_jitter_never_reaches_the_key(self) -> None:
        """The trap this design exists to avoid.

        If jitter entered `Fire.key`, two dispatchers would compute different
        keys for one occurrence and the idempotency defence would evaporate —
        an option added to smooth load would have silently disabled the
        guarantee that a cron fires once.
        """
        fire = Fire("trg_abc", "hourly_report", NINE_AM)

        # The key is the schedule's moment and the trigger, and nothing else.
        assert fire.key == f"trg_abc@{NINE_AM.isoformat()}"

        # And it does not move when the policy does: a dispatcher configured
        # with a different jitter still computes the same identity.
        assert _is_ready(fire, self._record(0), NINE_AM) is True
        assert fire.key == Fire("trg_abc", "hourly_report", NINE_AM).key

    def test_different_occurrences_get_different_delays(self) -> None:
        """Otherwise every trigger in a fleet shifts by the same amount and the
        herd is merely late rather than spread."""
        record = self._record(3600)
        moment = NINE_AM + timedelta(minutes=30)
        outcomes = {
            _is_ready(
                Fire(f"trg_{n}", "hourly_report", NINE_AM), record, moment
            )
            for n in range(40)
        }

        assert outcomes == {True, False}


class TestClaimingAcrossConnections:
    """Two store objects on one SQLite file — which is what two workers are.

    The conformance suite opens one store per backend, so it can only exercise
    the exclusivity a single object provides through its own mutex. What
    `BEGIN IMMEDIATE` actually buys is exclusivity between *connections*, and
    that needs two of them pointed at one file to show up at all.
    """

    async def test_two_connections_do_not_both_claim_one_trigger(
        self, tmp_path
    ) -> None:
        from loom.core.models import TriggerRecord

        db = str(tmp_path / "claims.db")
        left, right = SQLiteStore(db), SQLiteStore(db)

        await left.save_trigger(
            TriggerRecord(
                trigger_id="trg_shared",
                workflow="hourly_report",
                spec={"cron": "0 * * * *"},
                next_fire_at=NINE_AM,
            )
        )

        batches = await asyncio.gather(
            left.claim_due_triggers(NINE_AM, owner="left"),
            right.claim_due_triggers(NINE_AM, owner="right"),
            return_exceptions=True,
        )

        won = [
            trigger
            for batch in batches
            if isinstance(batch, list)
            for trigger in batch
        ]
        assert len(won) == 1, (
            f"both connections claimed the same trigger: "
            f"{[t.claimed_by for t in won]}"
        )


class TestOneSchedulerLoop:
    """`start_scheduler` drives cron too, under the same lease.

    Leaving the dispatcher outside it was a trap rather than an omission: a
    host calling `start_scheduler(elector=…)` reasonably believed its
    scheduling was single-leader, and its timers were while its crons were not.
    """

    async def test_the_scheduler_loop_fires_a_due_trigger(
        self, tmp_path
    ) -> None:
        db = str(tmp_path / "loop.db")
        clock = ManualClock(NINE_AM)
        rt = a_runtime(db, clock)
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(hourly_report)

        await rt.start_scheduler(interval=0.01, dispatcher=dispatcher)
        try:
            clock.advance(seconds=3600)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if await rt.store.list_executions(workflow="hourly_report"):
                    break
        finally:
            await rt.shutdown()

        runs = await rt.store.list_executions(workflow="hourly_report")
        assert len(runs) == 1, "the scheduler loop did not fire the cron"

    async def test_without_a_dispatcher_the_loop_is_unchanged(
        self, tmp_path
    ) -> None:
        """The default stays exactly what it was: timers and orphans only."""
        db = str(tmp_path / "noloop.db")
        clock = ManualClock(NINE_AM)
        rt = a_runtime(db, clock)
        await TriggerDispatcher(rt).register(hourly_report)

        await rt.start_scheduler(interval=0.01)
        try:
            clock.advance(seconds=3600)
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            await rt.shutdown()

        assert await rt.store.list_executions(workflow="hourly_report") == []


class TestClaimingAvoidsDuplicatedWork:
    """The half of the job the occurrence key does not do.

    The key means two dispatchers produce one *run*. It does not stop them both
    building the occurrence, both submitting it, and both advancing the record
    — so every test about run counts passes whether or not the dispatcher
    claims. This measures the thing that actually changes: how many dispatchers
    did the work.
    """

    async def test_only_one_of_two_dispatchers_does_the_work(
        self, tmp_path
    ) -> None:
        db = str(tmp_path / "work.db")
        clock = ManualClock(NINE_AM)
        first, second = a_runtime(db, clock), a_runtime(db, clock)
        await TriggerDispatcher(first).register(hourly_report)

        at_ten = NINE_AM + timedelta(hours=1)
        batches = await asyncio.gather(
            TriggerDispatcher(first).tick(at_ten),
            TriggerDispatcher(second).tick(at_ten),
        )

        did_work = [batch for batch in batches if batch]
        assert len(did_work) == 1, (
            f"{len(did_work)} dispatchers each submitted the occurrence; the "
            "key collapsed them into one run, but the work was done twice"
        )

    async def test_a_claim_does_not_strand_the_trigger_when_a_tick_dies(
        self, tmp_path
    ) -> None:
        """A lease, not a lock.

        The dispatcher that claimed is gone and never advanced the record. The
        occurrence must still be reachable once the lease lapses, or one crash
        costs every future run rather than one late one.
        """
        db = str(tmp_path / "stranded.db")
        clock = ManualClock(NINE_AM)
        rt = a_runtime(db, clock)
        await TriggerDispatcher(rt).register(hourly_report)

        at_ten = NINE_AM + timedelta(hours=1)
        # Claim with a short lease and then vanish, advancing nothing.
        await rt.store.claim_due_triggers(at_ten, owner="ghost", lease_seconds=30)

        blocked = await TriggerDispatcher(rt).tick(at_ten)
        recovered = await TriggerDispatcher(rt).tick(at_ten + timedelta(seconds=31))

        assert blocked == [], "a live claim was ignored"
        assert len(recovered) == 1, "the trigger was stranded by a dead claim"
