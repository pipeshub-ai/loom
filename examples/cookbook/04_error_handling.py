"""Example 4 — Error handling: retry, fallback, and OnError.

Shows three resilience patterns:
  - @step with Retry (exponential back-off) for transient failures
  - OnError.ROUTE to get a Failure value instead of raising
  - Conditional branching on whether a step succeeded or fell back

Demonstrates: Retry, Failure, OnError, RetriesExhausted.

Run:
    python3 examples/cookbook/04_error_handling.py
"""

from __future__ import annotations

from loom import Context, Failure, OnError, Retry, Runtime, step, workflow
from loom.stores.memory import MemoryStore

# Simulate a flaky service that eventually succeeds
_call_count: dict[str, int] = {}


@step(retry=Retry(max_attempts=4, initial_delay=0.1, max_delay=1.0))
async def flaky_api_call(service: str) -> str:
    """Calls a service that fails the first two times."""
    _call_count[service] = _call_count.get(service, 0) + 1
    n = _call_count[service]
    if n < 3:
        print(f"  [flaky_api_call] attempt {n} — failing…")
        msg = f"Service {service} temporarily unavailable"
        raise ConnectionError(msg)
    print(f"  [flaky_api_call] attempt {n} — success!")
    return f"data from {service}"


@step(retry=Retry(max_attempts=2, initial_delay=0.05))
async def always_fails(label: str) -> str:
    """A step that never succeeds — used to show the fallback path."""
    msg = f"{label} is permanently down"
    raise RuntimeError(msg)


@step
async def aggregate(primary: str, fallback_used: bool) -> dict:
    """Combine primary result with metadata."""
    return {
        "primary": primary,
        "fallback_used": fallback_used,
        "status": "partial" if fallback_used else "full",
    }


@workflow(name="resilient_pipeline")
async def resilient_pipeline(ctx: Context, service: str) -> dict:
    """Run a pipeline that handles flaky and permanently failing steps."""
    # Retries automatically — succeeds on the 3rd attempt
    primary = await ctx.step(flaky_api_call, service)

    # OnError.ROUTE returns a Failure dataclass instead of raising
    result = await ctx.step(always_fails, "backup-service", on_error=OnError.ROUTE)
    fallback_used = isinstance(result, Failure)
    if fallback_used:
        print(f"  [workflow] Step failed: {result.message} — using fallback")

    return await ctx.step(aggregate, primary, fallback_used)


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    result = await rt.run(resilient_pipeline, "payments-api")

    print(f"\nStatus : {result.status.value}")
    print(f"Output : {result.output}")
    print(f"Run ID : {result.run_id}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
