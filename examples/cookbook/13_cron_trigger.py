"""Example 13 — Cron-triggered workflow.

A workflow that fires on a cron schedule — NOT via ctx.sleep().
The TriggerDispatcher scans registered triggers and creates runs
at the scheduled time.

Run:
    python3 examples/cookbook/13_cron_trigger.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.runtime.dispatcher import TriggerDispatcher
from workflow_builder.state.memory import MemoryStore
from workflow_builder.triggers.specs import Schedule


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

    rt = Runtime(store=MemoryStore())
    dispatcher = TriggerDispatcher(rt)

    log("setup", "Registering heartbeat workflow (cron: */1 * * * *)")
    await dispatcher.register_workflow_async(heartbeat)

    log("setup", "Starting dispatcher (checking every 5s)")
    log("setup", "Will run for 2.5 minutes to catch 2-3 cron fires")
    await dispatcher.start(interval=5.0)

    # Let it run for 2.5 minutes (should fire 2-3 times)
    await asyncio.sleep(150)

    await dispatcher.stop()

    # Show the runs
    runs = await rt.list_runs(workflow="heartbeat")
    header("RUNS")
    for run in runs:
        log("run", f"{run.run_id}  status={run.status.value}")
    log("total", f"{len(runs)} heartbeat run(s) completed")


if __name__ == "__main__":
    asyncio.run(main())
