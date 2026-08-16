"""Example 2 — Parallel fan-out with ctx.gather().

Fetches multiple posts concurrently, then aggregates results.
Demonstrates: ctx.gather() for true parallelism, fan-in aggregation.

Run:
    python3 examples/cookbook/02_parallel.py
"""

from __future__ import annotations

import httpx

from loom import Context, Retry, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step(retry=Retry(max_attempts=2))
async def fetch_post(post_id: int) -> dict:
    """Fetch a single post — retried on transient network errors."""
    url = f"https://jsonplaceholder.typicode.com/posts/{post_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()


@step
async def build_digest(posts: list[dict]) -> str:
    """Combine all post titles into a numbered digest."""
    lines = [f"{i+1}. {p['title']}" for i, p in enumerate(posts)]
    return "\n".join(lines)


@workflow(name="parallel_digest")
async def parallel_digest(ctx: Context, post_ids: list[int]) -> str:
    """Fetch multiple posts in parallel, then build a digest."""
    # ctx.gather() runs all steps concurrently and returns results in order
    posts = await ctx.gather(*[ctx.step(fetch_post, pid) for pid in post_ids])
    digest = await ctx.step(build_digest, list(posts))
    return digest


async def main() -> None:
    async with Runtime(store=MemoryStore()) as rt:
        result = await rt.run(parallel_digest, [1, 2, 3, 4, 5])

        print(f"Status : {result.status.value}")
        print()
        print("=== Digest ===")
        print(result.output)
        print(f"\nRun ID : {result.run_id}")


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
