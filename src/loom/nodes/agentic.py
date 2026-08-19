"""``agent.*`` — the nodes that use judgement.

A separate category from ``control`` and ``transform`` on purpose. The choice
between ``control.switch`` and ``agent.classify`` is the choice LOOM's coding
agent already has to make on every node — *can I write a rule today that is
right for every input the spec allows?* — and putting the two in different
categories makes that choice a deliberate act rather than an accident of what a
model remembered.

The tell for a rule that should not be written is an invented constant: a
keyword list, a regex over prose, a threshold nobody supplied.
``if "urgent" in subject.lower()`` is a guess wearing the clothes of logic, and
``agent.classify`` is what it should have been.

Every node here is ``deterministic=False``: its answer is journaled, so a replay
returns what the run actually decided rather than re-asking a model that may now
answer differently.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from loom.nodes.base import Node, NodeContext
from loom.nodes.registry import register_node
from loom.nodes.spec import NodeCategory, NodeExample, NodeSpec

__all__ = ["ClassifyNode", "ExtractStructuredNode", "JudgeNode", "SummarizeNode"]

def agent_spec(**declared: Any) -> NodeSpec:
    """An ``agent.*`` spec. Never deterministic: the verdict is journaled so a
    replay returns what the run decided, not what a model would say now."""
    return NodeSpec(
        import_module="loom.nodes.agentic",
        category=NodeCategory.AGENT,
        deterministic=False,
        **declared,
    )


async def _ask(ctx: NodeContext, prompt: str) -> str:
    answer = await ctx.agent(prompt)
    return str(getattr(answer, "output", answer) or "").strip()


# ---------------------------------------------------------------------------


class ClassifyIn(BaseModel):
    text: str = Field(description="What is being classified.")
    labels: list[str] = Field(
        default_factory=list, description="The allowed labels. Required."
    )
    instructions: str = Field(
        default="", description="Extra guidance on where the boundaries are."
    )


class ClassifyOut(BaseModel):
    label: str = ""
    confident: bool = True
    """False when the model did not return one of the offered labels — reported
    rather than silently coerced, because a coerced label is a wrong branch that
    looks like a right one."""


@register_node
class ClassifyNode(Node[ClassifyIn, ClassifyOut]):
    """Put text into one of a fixed set of labels, using a model."""

    spec = agent_spec(
        id="agent.classify",
        open_world=True,
        summary="Assign text to one of a fixed set of labels, using a model.",
        description=(
            "Reach for this instead of a keyword list. If you can state the rule "
            "exactly, control.switch is free and deterministic."
        ),
        tags=["classify", "label", "categorise", "triage", "judgement"],
        examples=[
            NodeExample(
                payload={
                    "text": "The invoice is three weeks late and nobody replies.",
                    "labels": ["billing", "support", "sales"],
                }
            )
        ],
    )
    Input, Output = ClassifyIn, ClassifyOut

    async def run(self, ctx: NodeContext, payload: ClassifyIn) -> ClassifyOut:
        if not payload.labels:
            raise ValueError("agent.classify needs the set of labels it may choose from")
        answer = await _ask(
            ctx,
            "Classify the text below into exactly one label. Reply with the label "
            f"and nothing else.\n\nLabels: {', '.join(payload.labels)}\n"
            + (f"Guidance: {payload.instructions}\n" if payload.instructions else "")
            + f"\nText:\n{payload.text}",
        )
        cleaned = answer.strip().strip(".\"'")
        for label in payload.labels:
            if cleaned.lower() == label.lower():
                return ClassifyOut(label=label, confident=True)
        for label in payload.labels:
            if label.lower() in cleaned.lower():
                return ClassifyOut(label=label, confident=False)
        return ClassifyOut(label=payload.labels[0], confident=False)


# ---------------------------------------------------------------------------


class ExtractStructuredIn(BaseModel):
    text: str = Field(description="The prose to read.")
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        alias="fields",
        description="JSON Schema of what to pull out.",
    )
    instructions: str = Field(default="")
    model_config = {"populate_by_name": True}


class ExtractStructuredOut(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    parsed: bool = False
    raw: str = ""


@register_node
class ExtractStructuredNode(Node[ExtractStructuredIn, ExtractStructuredOut]):
    """Pull structured fields out of prose, using a model."""

    spec = agent_spec(
        id="agent.extract_structured",
        open_world=True,
        summary="Pull structured fields out of prose, using a model.",
        description=(
            "For text with no reliable shape. When the shape is reliable — an "
            "order id, a URL — transform.extract is free and exact."
        ),
        tags=["extract", "parse", "structured", "judgement"],
        examples=[
            NodeExample(
                payload={
                    "text": "Dana at Acme wants 40 seats by Q3.",
                    "fields": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "seats": {"type": "integer"},
                        },
                    },
                }
            )
        ],
    )
    Input, Output = ExtractStructuredIn, ExtractStructuredOut

    async def run(
        self, ctx: NodeContext, payload: ExtractStructuredIn
    ) -> ExtractStructuredOut:
        answer = await _ask(
            ctx,
            "Extract the fields described by this JSON Schema from the text. "
            "Reply with JSON only.\n\nSchema:\n"
            f"{json.dumps(payload.schema_, indent=2)}\n"
            + (f"\nGuidance: {payload.instructions}\n" if payload.instructions else "")
            + f"\nText:\n{payload.text}",
        )
        from loom.integrations.structured import extract_json

        parsed = extract_json(answer)
        if isinstance(parsed, dict):
            return ExtractStructuredOut(values=parsed, parsed=True, raw=answer)
        # Reported, never guessed. An empty dict with parsed=False is a fact the
        # caller can branch on; inventing fields would not be.
        return ExtractStructuredOut(values={}, parsed=False, raw=answer)


# ---------------------------------------------------------------------------


class SummarizeIn(BaseModel):
    text: str = Field(description="What to summarise.")
    style: str = Field(default="a short paragraph", description="Shape of the summary.")
    focus: str = Field(default="", description="What the summary should be about.")


class SummarizeOut(BaseModel):
    summary: str = ""


@register_node
class SummarizeNode(Node[SummarizeIn, SummarizeOut]):
    """Summarise text, using a model."""

    spec = agent_spec(
        id="agent.summarize",
        open_world=True,
        summary="Summarise text in a requested style and focus, using a model.",
        tags=["summarize", "digest", "condense", "judgement"],
        examples=[
            NodeExample(
                payload={"text": "...", "style": "three bullets",
                         "focus": "what changed and who is affected"}
            )
        ],
    )
    Input, Output = SummarizeIn, SummarizeOut

    async def run(self, ctx: NodeContext, payload: SummarizeIn) -> SummarizeOut:
        answer = await _ask(
            ctx,
            f"Summarise the text below as {payload.style}."
            + (f" Focus on {payload.focus}." if payload.focus else "")
            + f"\n\nText:\n{payload.text}",
        )
        return SummarizeOut(summary=answer)


# ---------------------------------------------------------------------------


class JudgeIn(BaseModel):
    candidate: str = Field(description="What is being judged.")
    criteria: str = Field(description="What makes it good or bad.")
    reference: str = Field(default="", description="What good looks like, if known.")


class JudgeOut(BaseModel):
    passed: bool = False
    score: float = 0.0
    reason: str = ""


@register_node
class JudgeNode(Node[JudgeIn, JudgeOut]):
    """Score something against stated criteria, using a model."""

    spec = agent_spec(
        id="agent.judge",
        open_world=True,
        summary="Score a candidate against stated criteria, using a model.",
        description=(
            "Returns a reason as well as a score, because a score nobody can "
            "explain is not reviewable — and this is usually deciding whether "
            "work goes out."
        ),
        tags=["judge", "evaluate", "score", "review", "judgement"],
        examples=[
            NodeExample(
                payload={
                    "candidate": "Hi — sorry for the delay, here is your refund.",
                    "criteria": "polite, states the amount, gives a timeline",
                }
            )
        ],
    )
    Input, Output = JudgeIn, JudgeOut

    async def run(self, ctx: NodeContext, payload: JudgeIn) -> JudgeOut:
        answer = await _ask(
            ctx,
            "Judge the candidate against the criteria. Reply with JSON: "
            '{"passed": bool, "score": 0-1, "reason": "..."}.\n\n'
            f"Criteria: {payload.criteria}\n"
            + (f"Reference: {payload.reference}\n" if payload.reference else "")
            + f"\nCandidate:\n{payload.candidate}",
        )
        from loom.integrations.structured import extract_json

        parsed = extract_json(answer)
        if isinstance(parsed, dict):
            return JudgeOut(
                passed=bool(parsed.get("passed", False)),
                score=float(parsed.get("score", 0.0) or 0.0),
                reason=str(parsed.get("reason", "")),
            )
        return JudgeOut(passed=False, score=0.0, reason=answer[:500])
