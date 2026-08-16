"""Example 3 — Durable sleep (delayed execution).

A workflow that does some work, sleeps durably for a configured
duration, then continues.  Unlike asyncio.sleep(), ctx.sleep() survives
process crashes — the wake time is journaled and the scheduler resumes
the run when the timer expires.

Demonstrates: ctx.sleep(), rt.start_scheduler(), rt.wait().

Run:
    python3 examples/cookbook/03_durable_sleep.py
"""

from __future__ import annotations

from datetime import timedelta

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def prepare_message(recipient: str) -> str:
    """Build a reminder message."""
    return f"Hi {recipient}, this is your scheduled reminder!"


@step
async def deliver_message(message: str) -> dict:
    """Simulate delivering the message (print to console)."""
    print(f"  >> Delivering: {message}")
    return {"delivered": True, "message": message}


@workflow(name="delayed_reminder")
async def delayed_reminder(ctx: Context, params: dict) -> dict:
    """Prepare a reminder, sleep, then deliver it."""
    recipient = params["recipient"]
    delay_seconds = params["delay_seconds"]

    message = await ctx.step(prepare_message, recipient)

    print(f"  [workflow] Sleeping for {delay_seconds}s …")
    await ctx.sleep(timedelta(seconds=delay_seconds))
    print("  [workflow] Awake — delivering now.")

    result = await ctx.step(deliver_message, message)
    return result


async def main() -> None:
    async with Runtime(store=MemoryStore()) as rt:
        # Scheduler polls every second so the run resumes promptly after sleep
        await rt.start_scheduler(interval=1.0)

        print("Starting delayed reminder workflow (5-second sleep)…")
        result = await rt.run(delayed_reminder, {"recipient": "Alice", "delay_seconds": 5})

        # If the runtime parked the run (sleep > inline threshold), wait for it
        if result.status.value == "suspended":
            print(f"  Run {result.run_id} suspended — waiting for scheduler to resume…")
            result = await rt.wait(result.run_id, timeout=30)

        print(f"\nStatus : {result.status.value}")
        print(f"Output : {result.output}")
        print(f"Run ID : {result.run_id}")


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
