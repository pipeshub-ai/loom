"""Example 22 — Writing and reusing your own node.

A ``@step`` is your function journaled. A **node** is a shareable contract: the
coding agent can find it, render the exact call to make, and check that call
before anyone runs it. The tell that you want one is the second caller — a step
copied into another workflow with the arguments shuffled is a node that has not
been written yet.

Authoring is one file. ``@register_node`` derives the schemas, the import line,
and the code the agent writes from — all from the class — so there is no
manifest to keep in step and nothing to declare twice.

Demonstrates: Node, NodeSpec, register_node, the rendered contract, the
catalog, and shipping one by entry point.

Run:
    python3 examples/cookbook/22_custom_nodes.py
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.nodes import (
    Node,
    NodeCategory,
    NodeExample,
    NodeRegistry,
    NodeSpec,
    register_node,
)
from workflow_builder.state.memory import MemoryStore

# ---------------------------------------------------------------------------
# The two models are the contract.
# ---------------------------------------------------------------------------


class SlaIn(BaseModel):
    """Describe every field — this text becomes the comment the agent reads."""

    opened_hours_ago: float = Field(description="How long the ticket has been open.")
    plan: str = Field(default="standard", description="standard | premium | enterprise")


class SlaOut(BaseModel):
    breached: bool
    hours_remaining: float
    reason: str = ""
    """Reported rather than derived. A breach nobody can explain is not
    reviewable, and this node decides whether somebody gets paged."""


# ---------------------------------------------------------------------------
# The node. Real work goes in a step; the body composes steps.
# ---------------------------------------------------------------------------

WINDOWS = {"standard": 48.0, "premium": 24.0, "enterprise": 4.0}


@step
async def sla_window(plan: str) -> float:
    """A lookup, which in a real system reads a table."""
    return WINDOWS.get(plan, WINDOWS["standard"])


@register_node
class SlaCheckNode(Node[SlaIn, SlaOut]):
    """Decide whether a ticket has breached its SLA."""

    spec = NodeSpec(
        id="custom.sla_check",
        version="1.0.0",
        category=NodeCategory.CONTROL,
        summary="Decide whether a ticket has breached its plan's SLA window.",
        # Deterministic: given the same input it answers the same forever, so
        # the engine may recompute it on replay rather than journal it. Say
        # False the moment a body calls a model or reads a policy that changes.
        deterministic=True,
        tags=["sla", "escalation", "support", "deadline"],
        examples=[
            NodeExample(
                title="A premium ticket nearing its window",
                payload={"opened_hours_ago": 20, "plan": "premium"},
            )
        ],
    )
    Input, Output = SlaIn, SlaOut

    async def run(self, ctx, payload: SlaIn) -> SlaOut:
        window = await ctx.step(sla_window, payload.plan)
        remaining = window - payload.opened_hours_ago
        return SlaOut(
            breached=remaining <= 0,
            hours_remaining=remaining,
            reason=f"{payload.plan} allows {window:g}h, open {payload.opened_hours_ago:g}h",
        )


@workflow(name="triage_ticket")
async def triage_ticket(ctx: Context, age_hours: float) -> str:
    sla = await ctx.node("custom.sla_check", SlaIn(opened_hours_ago=age_hours, plan="premium"))
    if sla.breached:
        return f"escalate: {sla.reason}"
    return f"ok, {sla.hours_remaining:g}h left"


async def main() -> None:
    from workflow_builder.nodes import get_node_catalog

    catalog = get_node_catalog()

    print("The node is discoverable the moment it is registered:\n")
    for card in catalog.search("sla escalation"):
        print(f"  {card.id:22} [{card.category}]  {card.summary}")

    print("\nAnd this is what the coding agent writes from — code, not schema:\n")
    print(catalog.contract("custom.sla_check"))

    runtime = Runtime(store=MemoryStore())
    runtime.register(triage_ticket)

    print("\nCalling it:\n")
    for age in (20.0, 30.0):
        result = await runtime.run(triage_ticket, age)
        print(f"  {age:>4}h open -> {result.output}")

    # The body's own step journals *beneath* the node, so a node adds packaging
    # and no new durability semantics.
    entries = await runtime.store.load_journal(result.run_id)
    print("\nJournal:")
    for entry in entries:
        print(f"  {entry.path:<5} {entry.kind.value:<6} {entry.name}")

    print("\nRegistration is global by default. Keep one local to a Runtime:")
    isolated = Runtime(store=MemoryStore(), nodes=NodeRegistry())
    isolated.nodes.register_node(SlaCheckNode)
    print(f"  local registry sees: {isolated.nodes.node_ids()}")

    print("\nTo ship it in a package, one entry point makes it installable:")
    print('  [project.entry-points.loom_node]')
    print('  sla_check = "my_package.nodes:SlaCheckNode"')


if __name__ == "__main__":
    asyncio.run(main())
