"""Example 5 — Human-in-the-loop (wait_for_event).

A content moderation workflow that:
  1. Receives a piece of content
  2. Runs an automated pre-check
  3. Suspends and waits for a human reviewer to approve or reject
  4. Continues with publish or discard based on the decision

Demonstrates: ctx.wait_for_event(), rt.send_event(), durable suspension.

Run:
    python3 examples/cookbook/05_human_in_the_loop.py
"""

from __future__ import annotations

import asyncio

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def auto_check(content: str) -> dict:
    """Run an automated content check (keyword filter)."""
    blocked_words = {"spam", "abuse", "hate"}
    found = [w for w in blocked_words if w in content.lower()]
    return {
        "passed": len(found) == 0,
        "flagged_words": found,
        "content": content,
    }


@step
async def publish(content: str) -> str:
    """Publish the approved content."""
    print(f"  >> Publishing: {content[:60]}…")
    return f"Published: {content[:60]}"


@step
async def discard(content: str, reason: str) -> str:
    """Discard the content and log the reason."""
    print(f"  >> Discarding content. Reason: {reason}")
    return f"Discarded. Reason: {reason}"


@workflow(name="content_moderation")
async def content_moderation(ctx: Context, content: str) -> str:
    """Moderate content with automated check + human review."""
    check = await ctx.step(auto_check, content)

    if not check["passed"]:
        # Auto-reject — no human needed
        return await ctx.step(
            discard, content, f"Auto-rejected: {check['flagged_words']}"
        )

    # Suspend and wait for a human reviewer
    print("  [workflow] Awaiting human review decision…")
    decision = await ctx.wait_for_event("review_decision")

    if decision.get("approved"):
        return await ctx.step(publish, content)
    else:
        reason = decision.get("reason", "Rejected by reviewer")
        return await ctx.step(discard, content, reason)


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    await rt.start_scheduler(interval=1.0)

    content = "Check out this amazing new product launch!"

    print(f"Submitting content for moderation: '{content}'")
    result = await rt.run(content_moderation, content)

    run_id = result.run_id
    print(f"Run ID  : {run_id}")
    print(f"Status  : {result.status.value}")

    if result.status.value == "suspended":
        print("\n  [main] Simulating human reviewer approving after 1 second…")
        await asyncio.sleep(1)

        # Human sends their decision as an event
        await rt.send_event(run_id, "review_decision", {"approved": True})

        result = await rt.wait(run_id, timeout=10)
        print(f"\nFinal Status : {result.status.value}")
        print(f"Final Output : {result.output}")

    await rt.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
