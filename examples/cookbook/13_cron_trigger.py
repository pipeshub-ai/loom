"""Example 13 — Cron-triggered workflow.

A workflow that fires on a cron schedule — NOT via ctx.sleep().
The TriggerDispatcher scans registered triggers and creates runs
at the scheduled time.

Run:
    python3 examples/cookbook/13_cron_trigger.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from loom import Context, Runtime, step, workflow
from loom.runtime.clock import ManualClock
from loom.runtime.dispatcher import TriggerDispatcher
from loom.stores.memory import MemoryStore
from loom.testing import advance
from loom.triggers.specs import Schedule

#: A fixed moment, so the run is reproducible rather than "whenever you ran it".
START = datetime(2026, 3, 2, 8, 59, tzinfo=UTC)


@step
async def get_time() -> str:
    """Return current time as a string."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%H:%M:%S")


@step
async def build_report(time_str: str) -> str:
    """Build a simple heartbeat report."""
    return f"Heartbeat at {time_str} - system is healthy"


@workflow(
    name="heartbeat",
    triggers=[Schedule("*/1 * * * *")],  # every minute
)
async def heartbeat(ctx: Context, _input: None = None) -> str:
    """Periodic health check that fires every minute via cron."""
    t = await ctx.step(get_time)
    report = await ctx.step(build_report, t)
    print(f"  >> {report}")
    return report


async def main() -> None:
    header("Cron Trigger Example")

    # Fixed start time, so the example prints the same thing every run.
    async with Runtime(store=MemoryStore(), clock=ManualClock(START)) as rt:
        dispatcher = TriggerDispatcher(rt)

        log("setup", "Registering heartbeat workflow (cron: */1 * * * *)")
        await dispatcher.register_workflow_async(heartbeat)

        # A ManualClock is the Runtime's own clock, so moving it moves everything
        # that reads the time — the dispatcher's schedule and any ctx.sleep alike.
        # `advance()` moves it, ticks both schedulers, and waits for the runs it
        # started, which is what "let a minute pass" actually has to mean.
        #
        # Real deployments pass no clock and call `dispatcher.start(interval=...)`.
        header("FIRING THE SCHEDULE")
        log("setup", "Advancing the clock a minute at a time")

        for _ in range(3):
            fired = await advance(rt, minutes=1, dispatcher=dispatcher)
            log("tick", f"{rt.clock.now():%H:%M} -> fired {len(fired)} run(s)")

        runs = await rt.list_runs(workflow="heartbeat")
        header("RUNS")
        for run in runs:
            log("run", f"{run.run_id[:16]}…  status={run.status.value}")
        log("total", f"{len(runs)} heartbeat run(s)")

        header("IN PRODUCTION")
        log("note", "await dispatcher.start(interval=5.0)   # scans every 5s")
        log("note", "The dispatcher is the only thing that needs to be running;")
        log("note", "the workflow itself has no timer and costs nothing idle.")


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
