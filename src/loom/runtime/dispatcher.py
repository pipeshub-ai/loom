"""Trigger dispatcher — fires cron/interval workflows on schedule.

Scans registered workflows for ``Schedule`` and ``Interval`` triggers,
computes next fire times, and creates runs at the scheduled moment
via ``Runtime.submit()``.

Usage::

    from loom.runtime.dispatcher import TriggerDispatcher

    rt = Runtime(store=MemoryStore())
    dispatcher = TriggerDispatcher(rt)
    await dispatcher.register(my_workflow)
    await dispatcher.start()
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loom.core.exceptions import ConfigurationError
from loom.core.models import TriggerKind, TriggerRecord
from loom.core.types import Duration, to_seconds
from loom.identity.facade import PRINCIPAL_KEY

if TYPE_CHECKING:
    from loom.runtime.engine import Runtime
    from loom.runtime.workflow import WorkflowDefinition
    from loom.stores.base import TriggerStore

logger = logging.getLogger("workflow.dispatcher")

# Trigger kinds the dispatcher can fire
_DISPATCHABLE_KINDS = {TriggerKind.SCHEDULE}

#: Backfill ceiling when a schedule does not name one. Ten is enough to cover
#: an ordinary restart or a short outage, and small enough that recovering from
#: a long one cannot itself become the incident.
_DEFAULT_MAX_CATCH_UP = 10


@dataclass(frozen=True)
class Fire:
    """One scheduled occurrence — the unit of dispatch.

    Identified by the moment the *schedule* called for, never by the wall clock
    that happened to notice it. That distinction is the whole idea: two
    dispatchers scanning two seconds apart, or one dispatcher retrying after a
    crash, must compute the same identity for the same occurrence, or the
    identity buys nothing.
    """

    trigger_id: str
    workflow: str
    scheduled_for: datetime

    @property
    def key(self) -> str:
        """The idempotency key this occurrence submits under.

        Deliberately contains nothing that varies between observers — no node
        id, no attempt number, and no jitter. Anything that did would make two
        processes disagree about whether they are looking at the same
        occurrence, which is the one thing this must never do.
        """
        return f"{self.trigger_id}@{self.scheduled_for.isoformat()}"


#: The parts of a trigger's description that decide *when* it fires.
#:
#: An allowlist rather than a denylist, and that is load-bearing: ``describe()``
#: also carries ``next_fire``, a timestamp computed at the moment it is called.
#: Hashing the whole description made a trigger's id depend on what time the
#: process booted, so two deployments minutes apart produced two ids for one
#: declared schedule — the duplication this id exists to prevent, reintroduced
#: by the fix for it. Anything not named here is either derived (``name``) or
#: policy (``catch_up``, ``max_catch_up``, ``jitter``), and policy must be able
#: to change without orphaning the trigger's accumulated state.
_IDENTIFYING_SPEC_FIELDS = ("kind", "cron", "timezone", "seconds")


def _trigger_id(workflow: str, spec: Any) -> str:
    """A stable id for one declared trigger.

    Derived from the workflow and from *when the trigger fires*, so the same
    declaration produces the same id in every process and on every boot, while
    a change to the schedule itself produces a different one — which is what
    lets registration tell "already known" from "this is new".

    Hashed rather than concatenated because a cron expression contains spaces,
    slashes, and asterisks, and a trigger id ends up in log lines, store keys,
    and the idempotency key of every run it fires.
    """
    described = spec.describe()
    material = json.dumps(
        {
            "workflow": workflow,
            "spec": {
                field: described[field]
                for field in _IDENTIFYING_SPEC_FIELDS
                if field in described
            },
        },
        sort_keys=True,
        default=str,
    )
    return "trg_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _occurrences_due(
    trigger: TriggerRecord, now: datetime
) -> tuple[list[Fire], int, datetime | None]:
    """What this trigger owes at *now*, what was dropped, and where it goes next.

    Pure, and deliberately so: the whole missed-fire policy is decided here from
    a record and a moment, which makes it directly testable without a store, a
    runtime, or a clock that has to be nudged.

    ``catch_up=False`` (the default, unchanged) fires the pending occurrence and
    skips the rest. ``catch_up=True`` fires every missed one, oldest first, each
    under its own key so a backfill interrupted halfway resumes without
    repeating what it already submitted.

    ``max_catch_up`` bounds the backfill, and the **newest** occurrences are the
    ones kept: coming back from a two-week outage, the last ten days of a daily
    report are worth more than the first ten. Without the bound, a per-minute
    schedule and a week of downtime is ten thousand runs submitted at once — a
    second outage caused by recovering from the first.
    """
    pending = trigger.next_fire_at
    if pending is None:
        return [], 0, None
    if pending.tzinfo is None:
        pending = pending.replace(tzinfo=UTC)
    if pending > now:
        return [], 0, pending

    spec = trigger.spec or {}
    catch_up = bool(spec.get("catch_up", False))
    ceiling = int(spec.get("max_catch_up", _DEFAULT_MAX_CATCH_UP))

    missed = [pending]
    cursor = pending
    # One past the ceiling is enough to know the backlog overflowed, and it
    # keeps a month of downtime on a per-minute cron from being walked in full
    # just to throw almost all of it away.
    guard = max(ceiling, 1) + 1 if catch_up else 1
    while len(missed) <= guard:
        following = _next_fire_from_record(trigger, cursor)
        if following is None:
            break
        if following.tzinfo is None:
            following = following.replace(tzinfo=UTC)
        if following <= cursor:
            logger.error(
                "Trigger %s does not advance past %s; leaving it there",
                trigger.trigger_id,
                cursor.isoformat(),
            )
            break
        cursor = following
        if following > now:
            break
        missed.append(following)

    next_fire, _ = _advance(trigger, missed[-1], now)

    if not catch_up:
        # The pending occurrence runs; anything else the outage covered is
        # skipped. `_advance` already counted and logged those.
        return [_fire_for(trigger, missed[0])], 0, next_fire

    dropped = max(0, len(missed) - ceiling)
    kept = missed[dropped:] if ceiling > 0 else []
    return [_fire_for(trigger, when) for when in kept], dropped, next_fire


def _fire_for(trigger: TriggerRecord, when: datetime) -> Fire:
    return Fire(
        trigger_id=trigger.trigger_id,
        workflow=trigger.workflow,
        scheduled_for=when,
    )


def _is_ready(fire: Fire, trigger: TriggerRecord, now: datetime) -> bool:
    """Whether jitter still holds this occurrence back.

    Jitter exists so a hundred triggers sharing ``0 0 * * *`` do not all submit
    in the same millisecond. It is applied here, to *when the dispatch happens*,
    and never to the schedule: the offset is derived from the occurrence's own
    key, so every process computes the same delay for the same occurrence and
    `Fire.key` stays exactly the schedule's moment.

    Sampling randomly instead would have been the trap. Two dispatchers would
    draw different delays, submit at different times under keys that are still
    identical — fine — but a delay that is not reproducible cannot be checked
    by the process that arrives second, so the occurrence would fire at the
    earliest draw rather than the agreed one. Deriving it keeps the decision
    identical everywhere without any coordination.
    """
    jitter = float((trigger.spec or {}).get("jitter", 0) or 0)
    if jitter <= 0:
        return True
    digest = hashlib.sha256(fire.key.encode()).digest()
    offset = (int.from_bytes(digest[:8], "big") / 2**64) * jitter
    return fire.scheduled_for + timedelta(seconds=offset) <= now


def _advance(
    trigger: TriggerRecord, fired_for: datetime, now: datetime
) -> tuple[datetime | None, int]:
    """Where this trigger fires next, and how many occurrences were skipped.

    Advancing from *fired_for* — the occurrence just handled — rather than from
    *now* keeps a schedule on its own grid. `Interval.next_fire` is
    ``after + every``, so passing the wall clock pushed every cycle out by
    however late that tick was, and the error accumulated: an hourly job
    noticed four minutes late became hourly-plus-four, then plus eight.

    Advancing that way can land in the past when the dispatcher was down for
    longer than one period, so the missed occurrences are then skipped to the
    next future one and *counted*. Skipping is the existing default and stays
    the default; what changes is that it stops being silent.
    """
    next_fire = _next_fire_from_record(trigger, fired_for)
    if next_fire is None:
        return None, 0
    if next_fire.tzinfo is None:
        next_fire = next_fire.replace(tzinfo=UTC)

    skipped = 0
    while next_fire <= now:
        following = _next_fire_from_record(trigger, next_fire)
        if following is None:
            return None, skipped
        if following.tzinfo is None:
            following = following.replace(tzinfo=UTC)
        if following <= next_fire:
            # A schedule that does not move forward would spin here forever.
            # Give up rather than hang the dispatcher for every other trigger.
            logger.error(
                "Trigger %s does not advance past %s; leaving it there",
                trigger.trigger_id,
                next_fire.isoformat(),
            )
            break
        next_fire = following
        skipped += 1

    return next_fire, skipped


class TriggerDispatcher:
    """Fires cron/interval workflows on schedule.

    Parameters
    ----------
    runtime:
        The ``Runtime`` that runs workflows.
    trigger_store:
        Optional persistent trigger store. Defaults to the runtime's
        store if it implements ``TriggerStore``, otherwise in-memory.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        trigger_store: TriggerStore | None = None,
        lease: Duration = 60.0,
    ) -> None:
        self._runtime = runtime
        # The Runtime's clock, so a ManualClock moves cron triggers too — a
        # dispatcher on wall time while the runtime is on virtual time would
        # be the one component a time-travel test could not reach.
        self._clock = runtime.clock
        self._store: Any = trigger_store or _resolve_store(runtime)
        self._lease = lease
        """How long a claim on a due trigger is held. Long enough to cover a
        tick, short enough that a dispatcher which dies mid-tick delays one
        occurrence rather than stranding the trigger."""
        self._task: asyncio.Task[None] | None = None

    async def register(
        self, defn: WorkflowDefinition[Any, Any, Any]
    ) -> int:
        """Extract triggers from a workflow and register them.

        Registers the workflow on the runtime and computes initial
        ``next_fire_at`` for each cron/interval trigger.

        Returns the number of triggers registered.
        """
        self._runtime.register(defn)
        count = 0
        declared: set[str] = set()

        for spec in defn.triggers:
            if spec.kind not in _DISPATCHABLE_KINDS:
                continue

            # Identity from what the trigger *is*, never from when it was
            # registered. Registration runs on every process start, so minting
            # a fresh id here added one more record per boot: after three
            # deploys the 10:00 report went out three times, from three rows
            # with three different ids that no occurrence key could collapse.
            trigger_id = _trigger_id(defn.name, spec)
            declared.add(trigger_id)

            existing = await self._store.get_trigger(trigger_id)
            if existing is not None:
                # Already known, and its schedule state is the truth. Recomputing
                # `next_fire_at` here would push the next fire out on every
                # boot, so a deployment that restarts more often than its
                # schedule fires would never fire at all — and it would look
                # like a broken cron rather than like the restarts.
                #
                # The *spec* is refreshed, though. Policy — catch-up, its
                # ceiling, jitter — is not part of the identity, so a change to
                # it keeps the same id and would otherwise never reach the
                # store: the code would say `catch_up=True` and the dispatcher
                # would go on reading `False` from a record written months ago.
                if existing.spec != spec.describe():
                    await self._store.save_trigger(
                        existing.model_copy(update={"spec": spec.describe()})
                    )
                count += 1
                continue

            next_fire = _next_fire_from_spec(spec, self._clock.now())
            if next_fire is None:
                continue

            # Ensure timezone-aware
            if next_fire.tzinfo is None:
                next_fire = next_fire.replace(tzinfo=UTC)

            trigger = TriggerRecord(
                trigger_id=trigger_id,
                workflow=defn.name,
                kind=spec.kind,
                spec=spec.describe(),
                next_fire_at=next_fire,
                timezone=getattr(spec, "timezone", "UTC"),
            )
            await self._store.save_trigger(trigger)
            count += 1
            logger.info(
                "Registered trigger %s for %s -> next %s",
                trigger.trigger_id,
                defn.name,
                next_fire.isoformat(),
            )

        await self._retire_undeclared(defn.name, declared)
        return count

    async def _retire_undeclared(self, workflow: str, declared: set[str]) -> None:
        """Drop stored triggers this workflow no longer declares.

        Triggers are declared in code, so code is the truth: changing a cron
        from hourly to daily has to stop the hourly one, or it fires forever as
        an orphan that appears in no source file and that nobody thinks to look
        for in the store.

        The reconcile is per workflow and only touches what this workflow
        previously declared. Worth knowing: two processes running *different
        versions* of the same workflow will each retire the other's trigger, so
        a rolling deploy that changes a schedule will flap until it completes.
        That is a bounded and self-correcting cost; an orphan firing forever is
        neither.
        """
        for stored in await self._store.list_triggers(workflow=workflow):
            if (
                stored.kind in _DISPATCHABLE_KINDS
                and stored.trigger_id not in declared
            ):
                await self._store.delete_trigger(stored.trigger_id)
                logger.info(
                    "Retired trigger %s for %s: no longer declared",
                    stored.trigger_id,
                    workflow,
                )

    # Keep old name as alias for backward compat
    register_workflow_async = register

    async def tick(
        self, now: datetime | None = None
    ) -> list[str]:
        """Fire due triggers. Returns list of created run IDs."""
        now = now or self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        due = await self._claim(now)
        run_ids: list[str] = []

        for trigger in due:
            if not trigger.enabled:
                continue

            # Check workflow still exists
            if trigger.workflow not in self._runtime._workflows:
                logger.warning(
                    "Trigger %s references unknown workflow %s, disabling",
                    trigger.trigger_id,
                    trigger.workflow,
                )
                await self._store.update_after_fire(
                    trigger.trigger_id, now, None
                )
                continue

            # Which occurrences this trigger owes, and where it goes next.
            # Both are computed from the schedule's own grid, so two
            # dispatchers noticing the same backlog agree on every occurrence
            # in it.
            fires, dropped, next_fire = _occurrences_due(trigger, now)

            fires = [f for f in fires if _is_ready(f, trigger, now)]
            if not fires:
                # Held back by jitter, and deliberately without advancing:
                # the occurrence is still owed, just not yet. The next tick
                # will find it again and compute the same delay.
                continue

            if dropped:
                logger.warning(
                    "Trigger %s dropped %d missed occurrence(s): more than "
                    "max_catch_up allows",
                    trigger.trigger_id,
                    dropped,
                )

            for fire in fires:
                run_id = await self._fire(fire)
                if run_id is None:
                    break
                run_ids.append(run_id)
            else:
                await self._store.update_after_fire(
                    trigger.trigger_id, fires[-1].scheduled_for, next_fire
                )
            continue

        # Also run the runtime's own timer tick (for ctx.sleep)
        await self._runtime.tick(now)
        return run_ids

    async def _claim(self, now: datetime) -> list[TriggerRecord]:
        """Take the due triggers this dispatcher will handle.

        Claiming is the *work* guarantee, not the correctness one: the
        occurrence key already means two dispatchers produce one run. What this
        adds is that they stop doing the same work twice and stop both
        advancing one record.

        Falls back to a plain read when the store predates the method — a host
        with its own ``TriggerStore`` should not be broken by a capability it
        has not implemented yet, and without a claim the behaviour is exactly
        what shipped before it existed.
        """
        claim = getattr(self._store, "claim_due_triggers", None)
        if claim is None:
            logger.debug(
                "%s has no claim_due_triggers; falling back to due_triggers",
                type(self._store).__name__,
            )
            return list(await self._store.due_triggers(now))
        return list(
            await claim(
                now,
                owner=self._runtime.node_id,
                lease_seconds=to_seconds(self._lease),
            )
        )

    async def _fire(self, fire: Fire) -> str | None:
        """Submit one occurrence. ``None`` when it could not be started.

        A failure here leaves the trigger unadvanced on purpose: the occurrence
        is still owed, and the next tick tries it again under the same key. The
        alternative — advancing past an occurrence that never ran — loses it
        silently, which is the one outcome a schedule must not have.
        """
        try:
            run_id: str = await self._runtime.submit(
                fire.workflow,
                trigger=TriggerKind.SCHEDULE,
                # The occurrence's identity, not this attempt's. Two
                # dispatchers racing, or one retrying after dying between the
                # submit and the advance, present the same key — and
                # `idempotency_key` is UNIQUE in every persistent store, so the
                # second resolves to the first's run instead of creating one.
                idempotency_key=fire.key,
                # No interactive caller to pin as owner — stamp the runtime's
                # service identity so an authenticated facade attributes this
                # run to the scheduler rather than leaving it ownerless.
                metadata={PRINCIPAL_KEY: self._runtime.service_principal.subject},
            )
        except Exception:
            logger.exception(
                "Failed to fire trigger %s for %s",
                fire.trigger_id,
                fire.workflow,
            )
            return None
        logger.info(
            "Fired %s for %s -> run %s",
            fire.trigger_id,
            fire.scheduled_for.isoformat(),
            run_id,
        )
        return run_id

    async def start(self, *, interval: float = 5.0) -> None:
        """Start background loop calling ``tick()`` periodically."""
        if self._task is not None:
            return

        async def loop() -> None:
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Dispatcher tick failed")
                await self._clock.sleep(interval)

        self._task = asyncio.create_task(loop())
        # So Runtime.shutdown() stops firing triggers before it drains the runs
        # already in flight. A dispatcher left running across a shutdown submits
        # work into a Runtime that is closing its store.
        self._runtime.supervise(self)
        logger.info("TriggerDispatcher started (interval=%.1fs)", interval)

    async def stop(self) -> None:
        """Stop the background loop."""
        self._runtime.unsupervise(self)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("TriggerDispatcher stopped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Every method a store must carry to hold schedules durably.
TRIGGER_STORE_METHODS = (
    "save_trigger",
    "get_trigger",
    "list_triggers",
    "due_triggers",
    "update_after_fire",
    "delete_trigger",
)


def _resolve_store(runtime: Runtime) -> Any:
    """The runtime's store, once it is established that it can hold schedules.

    This used to fall back to an in-memory store for anything that could not,
    which meant a durable deployment kept its runs in Postgres and its
    schedules in RAM — and lost every cron trigger on restart, with no error
    and no log line. A silent downgrade is worse than a refusal: the refusal is
    read once, the downgrade is discovered in production.

    Pass ``trigger_store=`` explicitly to opt into a non-durable one; a test
    that wants ephemeral schedules should have to say so.
    """
    store = getattr(runtime, "store", None)
    missing = [m for m in TRIGGER_STORE_METHODS if not hasattr(store, m)]
    if store is None or missing:
        raise ConfigurationError(
            f"{type(store).__name__} cannot persist schedules (missing "
            f"{', '.join(missing) or 'everything'}). Use a store that "
            "implements TriggerStore — Memory, SQLite, Postgres, or Mongo — or "
            "pass TriggerDispatcher(runtime, trigger_store=...) to choose one "
            "explicitly."
        )
    return store


def _next_fire_from_spec(
    spec: Any, after: datetime
) -> datetime | None:
    """Compute next fire time from a TriggerSpec."""
    if hasattr(spec, "next_fire"):
        return spec.next_fire(after)
    return None


def _next_fire_from_record(
    trigger: TriggerRecord, after: datetime
) -> datetime | None:
    """Compute next fire from a persisted trigger record."""
    spec_data = trigger.spec

    # Try Schedule (cron-based)
    cron_expr = spec_data.get("cron")
    if cron_expr:
        from loom.triggers.specs import Schedule

        tz = spec_data.get("timezone", trigger.timezone)
        sched = Schedule(cron_expr, timezone=tz)
        return sched.next_fire(after)

    # Try Interval (seconds-based)
    seconds = spec_data.get("seconds")
    if seconds:
        return after + timedelta(seconds=float(seconds))

    return None
