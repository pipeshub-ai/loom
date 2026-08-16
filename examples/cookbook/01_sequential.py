"""Example 1 — Sequential pipeline.

The simplest pattern: steps run one after another, each result
flowing into the next.  Demonstrates: @step, ctx.step(), Retry.

Run:
    python3 examples/cookbook/01_sequential.py
"""

from __future__ import annotations

import httpx

from loom import Context, Retry, Runtime, step, workflow
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# Steps — all I/O lives here, never in the workflow body
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, max_delay=10.0))
async def fetch_post(post_id: int) -> dict:
    """Fetch a post from JSONPlaceholder."""
    url = f"https://jsonplaceholder.typicode.com/posts/{post_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()


@step
async def extract_summary(post: dict) -> str:
    """Pull the title and first 80 chars of body."""
    title = post.get("title", "")
    body = post.get("body", "")[:80].replace("\n", " ")
    return f"{title} — {body}…"


@step
async def format_report(summary: str, post_id: int) -> str:
    """Wrap the summary in a simple report string."""
    return f"[Post #{post_id}] {summary}"


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="fetch_and_summarise")
async def fetch_and_summarise(ctx: Context, post_id: int) -> str:
    """Fetch a blog post and return a one-line summary."""
    post = await ctx.step(fetch_post, post_id)
    summary = await ctx.step(extract_summary, post)
    report = await ctx.step(format_report, summary, post_id)
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    async with Runtime(store=MemoryStore()) as rt:
        result = await rt.run(fetch_and_summarise, 1)

        print(f"Status : {result.status.value}")
        print(f"Output : {result.output}")
        print(f"Run ID : {result.run_id}")


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
