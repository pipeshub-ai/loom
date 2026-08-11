import asyncio

from workflow_builder.runtime import Context, Runtime, workflow
from workflow_builder.steps import step

calls: list[str] = []


@step(retry=2)
async def fetch(name: str) -> dict[str, str]:
    calls.append(name)
    return {"hello": name}


@step
async def flaky(n: int) -> int:
    calls.append(f"flaky{n}")
    if len([c for c in calls if c == f"flaky{n}"]) < 2:
        raise ConnectionError("boom")
    return n * 2


@workflow
async def greet(ctx: Context, name: str) -> dict:
    a = await ctx.step(fetch, name)
    b = await ctx.step(flaky, 21)
    parts = await ctx.gather(ctx.step(fetch, "x"), ctx.step(fetch, "y"), max_concurrency=2)
    await ctx.sleep(0.01)
    return {"a": a, "b": b, "parts": parts, "now": ctx.now().isoformat(), "id": ctx.uuid4()}


async def main() -> None:
    rt = Runtime()
    rt.register(greet)
    result = await rt.run(greet, "world")
    print("status:", result.status)
    print("output:", result.output)
    print("error:", result.error)
    print("calls:", calls)
    print("steps:", [(s.seq, s.name, s.status, s.attempts) for s in result.steps])


asyncio.run(main())
