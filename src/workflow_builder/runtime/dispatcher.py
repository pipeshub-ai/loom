"""Trigger dispatcher — fires cron/interval workflows on schedule.

Scans registered workflows for ``Schedule`` and ``Interval`` triggers,
computes next fire times, and creates runs at the scheduled moment
via ``Runtime.submit()``.

Usage::

    from workflow_builder.runtime.dispatcher import TriggerDispatcher

    rt = Runtime(store=MemoryStore())
    dispatcher = TriggerDispatcher(rt)
    await dispatcher.register(my_workflow)
    await dispatcher.start()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from workflow_builder.core.ids import new_id
from workflow_builder.core.models import TriggerKind, TriggerRecord
from workflow_builder.state.memory import MemoryStore

if TYPE_CHECKING:
    from workflow_builder.runtime.engine import Runtime
    from workflow_builder.runtime.workflow import WorkflowDefinition
    from workflow_builder.state.base import TriggerStore

logger = logging.getLogger("workflow.dispatcher")

# Trigger kinds the dispatcher can fire
_DISPATCHABLE_KINDS = {TriggerKind.SCHEDULE}


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
    ) -> None:
        self._runtime = runtime
        self._store: Any = trigger_store or _resolve_store(runtime)
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

        for spec in defn.triggers:
            if spec.kind not in _DISPATCHABLE_KINDS:
                continue

            next_fire = _next_fire_from_spec(spec, datetime.now(UTC))
            if next_fire is None:
                continue

            # Ensure timezone-aware
            if next_fire.tzinfo is None:
                next_fire = next_fire.replace(tzinfo=UTC)

            trigger = TriggerRecord(
                trigger_id=new_id("trg"),
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
        return count

    # Keep old name as alias for backward compat
    register_workflow_async = register

    async def tick(
        self, now: datetime | None = None
    ) -> list[str]:
        """Fire due triggers. Returns list of created run IDs."""
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        due = await self._store.due_triggers(now)
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

            try:
                run_id = await self._runtime.submit(
                    trigger.workflow,
                    trigger=TriggerKind.SCHEDULE,
                )
                run_ids.append(run_id)
                logger.info(
                    "Fired %s -> run %s",
                    trigger.trigger_id,
                    run_id,
                )
            except Exception:
                logger.exception(
                    "Failed to fire trigger %s for %s",
                    trigger.trigger_id,
                    trigger.workflow,
                )
                continue

            # Compute next fire time
            next_fire = _next_fire_from_record(trigger, now)
            if next_fire and next_fire.tzinfo is None:
                next_fire = next_fire.replace(tzinfo=UTC)

            await self._store.update_after_fire(
                trigger.trigger_id, now, next_fire
            )

        # Also run the runtime's own timer tick (for ctx.sleep)
        await self._runtime.tick(now)
        return run_ids

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
                await asyncio.sleep(interval)

        self._task = asyncio.create_task(loop())
        logger.info("TriggerDispatcher started (interval=%.1fs)", interval)

    async def stop(self) -> None:
        """Stop the background loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("TriggerDispatcher stopped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_store(runtime: Runtime) -> Any:
    """Get a TriggerStore from the runtime's store, or create one."""
    store = getattr(runtime, "store", None)
    if store is not None and hasattr(store, "save_trigger"):
        return store
    return MemoryStore()


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
        from workflow_builder.triggers.specs import Schedule

        tz = spec_data.get("timezone", trigger.timezone)
        sched = Schedule(cron_expr, timezone=tz)
        return sched.next_fire(after)

    # Try Interval (seconds-based)
    seconds = spec_data.get("seconds")
    if seconds:
        return after + timedelta(seconds=float(seconds))

    return None
