"""Example 21 — Guardrail nodes.

Guardrails used to run in exactly one place: around an agent's tool calls. The
abstraction was right and its reach was one call site, so a workflow could not
gate a plain ``ctx.step()`` on a policy, or check data crossing between steps.

The four verdicts are unchanged — ALLOW, REJECT, REPLACE, TRIPWIRE. What is new
is where they attach: standalone, around a node, and where they already ran.

One semantic differs outside an agent loop. There, REJECT hands the model an
explanation so it can adapt; here there is nobody to adapt, so a falsy verdict a
caller could ignore would let the guarded work proceed anyway. **REJECT raises.**

Demonstrates: ctx.guard(), guards= on a node, REPLACE redaction, TRIPWIRE.

Run:
    python3 examples/cookbook/21_guardrail_nodes.py
"""

from __future__ import annotations

from loom import Context, Runtime, step, workflow
from loom.core.exceptions import GuardrailTripwire
from loom.nodes import GuardrailRejected
from loom.nodes.guard import PiiIn, PolicyIn
from loom.stores.memory import MemoryStore


@step
async def publish(text: str) -> str:
    """Stand-in for the thing you would not want to do twice, or wrongly."""
    return f"published: {text}"


@workflow(name="redact_then_publish")
async def redact_then_publish(ctx: Context, draft: str) -> str:
    """REPLACE: the guard rewrites the value and the run continues.

    ``ctx.guard`` returns what the run should use — the original for ALLOW, the
    substitute for REPLACE. That is why it is worth assigning: a guard that
    returned a verdict nobody applied would be decoration.
    """
    safe = await ctx.guard(
        "guard.pii", PiiIn(value=draft, kinds=["api_key", "credit_card"], redact=True)
    )
    return await ctx.step(publish, safe)


@workflow(name="blocked_publish")
async def blocked_publish(ctx: Context, draft: str) -> str:
    """REJECT: raises, so the guarded step is never reached."""
    checked = await ctx.guard(
        "guard.policy",
        PolicyIn(
            value=draft,
            deny_if=["DROP TABLE", "rm -rf"],
            message="that looks like an injected command",
        ),
    )
    return await ctx.step(publish, checked)


@workflow(name="guarded_node")
async def guarded_node(ctx: Context, rows: list[str]) -> int:
    """A guard attached to a node runs *before* the node body.

    Declared per call here; ``NodeSpec.guards`` attaches one permanently to a
    node, which is how a node author ships a check with the node.
    """
    batched = await ctx.node(
        "control.batch", {"items": rows, "size": 2}, guards=["guard.schema"]
    )
    return batched.count


async def main() -> None:
    async with Runtime(store=MemoryStore()) as runtime:
        for flow in (redact_then_publish, blocked_publish, guarded_node):
            runtime.register(flow)

        print("REPLACE — the secret is rewritten and the run continues:")
        result = await runtime.run(
            redact_then_publish, "deploy with sk-abcdefghijklmnop1234 tonight"
        )
        print(f"  {result.status.value}: {result.output}\n")

        print("ALLOW — nothing to redact, the value passes through untouched:")
        result = await runtime.run(redact_then_publish, "deploy tonight")
        print(f"  {result.status.value}: {result.output}\n")

        print("REJECT — raises, so `publish` is never called:")
        result = await runtime.run(blocked_publish, "'; DROP TABLE users; --")
        print(f"  {result.status.value}: {result.error.message}")
        print(f"  the failure is a {GuardrailRejected.__name__}, not a quiet False\n")

        print("A guard attached to a node runs before the node body:")
        result = await runtime.run(guarded_node, ["a", "b", "c", "d", "e"])
        print(f"  {result.status.value}: {result.output} batches\n")

        print("TRIPWIRE aborts the run outright — reserve it for policy violations:")
        from loom.nodes.guard import GuardVerdict, enforce

        try:
            enforce(GuardVerdict.tripwire("exfiltration attempt"), guard="demo", value=1)
        except GuardrailTripwire as stopped:
            print(f"  {stopped}")

        print("\nA guard that *raises* is a tripwire too — a check that cannot run")
        print("has found nothing, and must not be read as having found nothing wrong.")


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
