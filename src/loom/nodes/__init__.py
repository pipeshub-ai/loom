"""Typed nodes: Pydantic in, Pydantic out.

A node is a reusable, versioned, catalogued unit of workflow work. Declare two
models and one method, and the node becomes searchable by the coding agent,
renderable as copy-pasteable code, fake-able in the smoke sandbox, and callable
from any workflow::

    from pydantic import BaseModel
    from loom.nodes import Node, NodeCategory, NodeSpec, register_node

    class ScoreIn(BaseModel):
        text: str
        threshold: float = 0.5

    class ScoreOut(BaseModel):
        score: float
        passed: bool

    @register_node
    class LeadScoreNode(Node[ScoreIn, ScoreOut]):
        spec = NodeSpec(id="custom.lead_score", category=NodeCategory.TRANSFORM,
                        summary="Score a lead from its description text.")
        Input, Output = ScoreIn, ScoreOut

        async def run(self, ctx, payload: ScoreIn) -> ScoreOut:
            score = await ctx.step(compute_score, payload.text)
            return ScoreOut(score=score, passed=score >= payload.threshold)

Then, from a workflow::

    result = await ctx.node("custom.lead_score", ScoreIn(text=lead.blurb))

Nodes add no durability semantics. ``ctx.node()`` journals exactly what the
equivalent hand-written code would, so deleting this package could not change
how an existing workflow replays.
"""

from __future__ import annotations

from loom.nodes.base import Node, NodeContext
from loom.nodes.catalog import NodeCard, NodeCatalog, NodeDetail
from loom.nodes.errors import (
    GuardrailRejected,
    HumanChannelMissing,
    HumanRequestExpired,
    NodeContractError,
    NodeNotFound,
)
from loom.nodes.registry import (
    NodeRegistry,
    get_node_catalog,
    load_builtin_nodes,
    load_node_entry_points,
    register_node,
)
from loom.nodes.spec import (
    EffectClass,
    NodeCategory,
    NodeExample,
    NodeSpec,
)

__all__ = [
    "EffectClass",
    "GuardrailRejected",
    "HumanChannelMissing",
    "HumanRequestExpired",
    "Node",
    "NodeCard",
    "NodeCatalog",
    "NodeCategory",
    "NodeContext",
    "NodeContractError",
    "NodeDetail",
    "NodeExample",
    "NodeNotFound",
    "NodeRegistry",
    "NodeSpec",
    "get_node_catalog",
    "load_builtin_nodes",
    "load_node_entry_points",
    "register_node",
]
