"""The built-in ``human.*`` nodes.

Five entries, one mechanism. ``human.form`` is the general case and the other
four are it with a fixed response model — which is worth the four files because
a catalog entry named ``approval`` is findable and ``form(response_model=...)``
is not.

All five park the run, cost nothing while parked, and resume typed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from loom.core.exceptions import ApprovalRejected
from loom.nodes.base import Node, NodeContext
from loom.nodes.errors import HumanRequestExpired
from loom.nodes.human.asking import TimeoutPolicy, ask
from loom.nodes.human.attest import ATTESTED_KEY, responder_of
from loom.nodes.registry import register_node
from loom.nodes.spec import (
    EffectClass,
    NodeCategory,
    NodeDuration,
    NodeExample,
    NodeSpec,
)

__all__ = [
    "ApprovalIn",
    "ApprovalNode",
    "ApprovalOut",
    "ChoiceIn",
    "ChoiceNode",
    "ChoiceOut",
    "EscalateIn",
    "EscalateNode",
    "EscalateOut",
    "FormIn",
    "FormNode",
    "FormOut",
    "ReviewIn",
    "ReviewNode",
    "ReviewOut",
]

def human_spec(**declared: Any) -> NodeSpec:
    """A ``human.*`` spec with the five constants filled in.

    A factory rather than a shared dict splatted into each spec: ``**_HUMAN``
    hides every keyword from the type checker, so a typo in one node's spec
    reads as an untyped ``object`` and lands at runtime.
    """
    return NodeSpec(
        import_module="loom.nodes.human",
        category=NodeCategory.HUMAN,
        suspends=True,
        deterministic=False,
        effect=EffectClass.WRITE,
        requires=["human_channel"],
        **declared,
    )


# ---------------------------------------------------------------------------
# human.approval
# ---------------------------------------------------------------------------


class ApprovalIn(BaseModel):
    subject: str = Field(description="Identifies this decision within the run.")
    prompt: str = Field(default="", description="What the person is being asked.")
    context: dict[str, Any] = Field(
        default_factory=dict, description="What they need in order to decide."
    )
    assignees: list[str] = Field(
        default_factory=list, description="Who is being asked."
    )
    timeout: NodeDuration | None = Field(
        default=None, description="Seconds or a timedelta. None waits forever."
    )
    on_timeout: str = Field(
        default=TimeoutPolicy.REJECT,
        description="reject | approve | fail — what an unanswered request means.",
    )
    live_view_url: str = Field(
        default="",
        description=(
            "Where the person can watch or take over a browser this run is "
            "holding — pass `page.session.live_view_url` from browser.navigate "
            "with scope='durable'. Threaded explicitly rather than discovered, "
            "so a human node stays independent of whether a browser exists."
        ),
    )


class ApprovalOut(BaseModel):
    approved: bool
    responder: str = ""
    comment: str = ""
    decided_at: datetime | None = None
    timed_out: bool = False


@register_node
class ApprovalNode(Node[ApprovalIn, ApprovalOut]):
    """Ask a person to approve or reject, and park until they answer."""

    spec = human_spec(
        id="human.approval",
        open_world=True,
        summary="Ask a person to approve or reject; park until they answer.",
        description=(
            "Resolved by `loom respond <run> <subject> --approve`, by "
            "runtime.approve(run_id, subject), or by POST /human/{id}/respond."
        ),
        tags=["approval", "gate", "sign-off", "hitl"],
        examples=[
            NodeExample(
                title="Gate a refund",
                payload={
                    "subject": "refund-4821",
                    "prompt": "Approve a $420 refund for order 4821?",
                    "context": {"amount": 420, "order": "4821"},
                    "assignees": ["finance@acme.com"],
                    "timeout": 86400,
                    "on_timeout": "reject",
                },
                notes="An unanswered request rejects, so a silent channel fails closed.",
            )
        ],
    )
    Input, Output = ApprovalIn, ApprovalOut

    async def run(self, ctx: NodeContext, payload: ApprovalIn) -> ApprovalOut:
        answer, timed_out, _ = await ask(
            ctx,
            node_id=self.spec.id,
            subject=payload.subject,
            prompt=payload.prompt or f"Approve {payload.subject}?",
            context=payload.context,
            assignees=payload.assignees,
            response_schema=ApprovalOut.model_json_schema(),
            timeout=payload.timeout,
            live_view_url=payload.live_view_url,
        )
        if timed_out:
            if payload.on_timeout == TimeoutPolicy.FAIL:
                raise HumanRequestExpired(
                    f"nobody answered {payload.subject!r} within {payload.timeout}, "
                    "and on_timeout='fail'"
                )
            return ApprovalOut(
                approved=payload.on_timeout == TimeoutPolicy.APPROVE,
                comment=f"no answer within {payload.timeout}",
                decided_at=ctx.now(),
                timed_out=True,
            )
        return _approval_from(answer, decided_at=ctx.now())


def _approval_from(answer: Any, *, decided_at: datetime) -> ApprovalOut:
    """Accept what a channel plausibly sends without accepting nonsense.

    ``runtime.approve`` sends ``{"approved": bool}``; a richer channel sends a
    responder and a comment; a minimal one sends a bare bool. Anything else is a
    validation error rather than a guess, because guessing here decides whether
    a refund goes out.
    """
    if isinstance(answer, bool):
        return ApprovalOut(approved=answer, decided_at=decided_at)
    if isinstance(answer, dict):
        return ApprovalOut(
            approved=bool(answer.get("approved", False)),
            responder=responder_of(answer),
            comment=str(answer.get("comment", "")),
            decided_at=answer.get("decided_at") or decided_at,
        )
    return ApprovalOut(approved=bool(answer), decided_at=decided_at)


# ---------------------------------------------------------------------------
# human.choice
# ---------------------------------------------------------------------------


class ChoiceIn(BaseModel):
    subject: str = Field(description="Identifies this decision within the run.")
    prompt: str = Field(default="", description="What the person is choosing between.")
    options: list[str] = Field(default_factory=list, description="The options offered.")
    allow_multiple: bool = Field(default=False, description="Accept more than one.")
    context: dict[str, Any] = Field(default_factory=dict)
    assignees: list[str] = Field(default_factory=list)
    timeout: NodeDuration | None = None
    default: list[str] = Field(
        default_factory=list, description="Selected if nobody answers in time."
    )


class ChoiceOut(BaseModel):
    selected: list[str] = Field(default_factory=list)
    responder: str = ""
    timed_out: bool = False


@register_node
class ChoiceNode(Node[ChoiceIn, ChoiceOut]):
    """Ask a person to pick from a list, and park until they answer."""

    spec = human_spec(
        id="human.choice",
        open_world=True,
        summary="Ask a person to pick from a list of options.",
        tags=["choice", "select", "triage", "hitl"],
        examples=[
            NodeExample(
                payload={
                    "subject": "route-ticket",
                    "prompt": "Which team should own this?",
                    "options": ["billing", "platform", "support"],
                },
            )
        ],
    )
    Input, Output = ChoiceIn, ChoiceOut

    async def run(self, ctx: NodeContext, payload: ChoiceIn) -> ChoiceOut:
        answer, timed_out, _ = await ask(
            ctx,
            node_id=self.spec.id,
            subject=payload.subject,
            prompt=payload.prompt or "Choose one:",
            context={**payload.context, "options": payload.options,
                     "allow_multiple": payload.allow_multiple},
            assignees=payload.assignees,
            response_schema=ChoiceOut.model_json_schema(),
            timeout=payload.timeout,
        )
        if timed_out:
            return ChoiceOut(selected=list(payload.default), timed_out=True)

        selected = _selected_from(answer)
        unknown = [s for s in selected if payload.options and s not in payload.options]
        if unknown:
            # A choice outside the offered set is not a choice. Accepting it
            # would let a channel widen the workflow's branch set from outside.
            raise HumanRequestExpired(
                f"{payload.subject}: {unknown} is not among the offered options "
                f"{payload.options}"
            )
        if not payload.allow_multiple and len(selected) > 1:
            raise HumanRequestExpired(
                f"{payload.subject}: allow_multiple is False and {len(selected)} "
                "options came back"
            )
        return ChoiceOut(
            selected=selected,
            responder=responder_of(answer),
        )


def _selected_from(answer: Any) -> list[str]:
    if isinstance(answer, str):
        return [answer]
    if isinstance(answer, list):
        return [str(x) for x in answer]
    if isinstance(answer, dict):
        chosen = answer.get("selected", answer.get("choice"))
        if isinstance(chosen, str):
            return [chosen]
        if isinstance(chosen, list):
            return [str(x) for x in chosen]
    return []


# ---------------------------------------------------------------------------
# human.form — the general case
# ---------------------------------------------------------------------------


class FormIn(BaseModel):
    subject: str = Field(description="Identifies this request within the run.")
    prompt: str = Field(default="", description="What is being asked for.")
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        alias="fields",
        description="JSON Schema of the answer. The channel renders a form from it.",
    )
    context: dict[str, Any] = Field(default_factory=dict)
    assignees: list[str] = Field(default_factory=list)
    timeout: NodeDuration | None = None

    model_config = {"populate_by_name": True}


class FormOut(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    responder: str = ""
    timed_out: bool = False


@register_node
class FormNode(Node[FormIn, FormOut]):
    """Ask a person to fill in a structured answer."""

    spec = human_spec(
        id="human.form",
        open_world=True,
        summary="Ask a person for a structured answer described by a JSON Schema.",
        description=(
            "The general case the other human.* nodes specialise. Reach for one "
            "of those first — they are findable by name and validate their own "
            "answer shape."
        ),
        tags=["form", "input", "collect", "hitl"],
        examples=[
            NodeExample(
                payload={
                    "subject": "shipping-details",
                    "prompt": "Where should this ship?",
                    "fields": {
                        "type": "object",
                        "properties": {"address": {"type": "string"}},
                        "required": ["address"],
                    },
                }
            )
        ],
    )
    Input, Output = FormIn, FormOut

    async def run(self, ctx: NodeContext, payload: FormIn) -> FormOut:
        answer, timed_out, _ = await ask(
            ctx,
            node_id=self.spec.id,
            subject=payload.subject,
            prompt=payload.prompt or f"Complete {payload.subject}",
            context=payload.context,
            assignees=payload.assignees,
            response_schema=payload.schema_,
            timeout=payload.timeout,
        )
        if timed_out:
            return FormOut(timed_out=True)
        if isinstance(answer, dict):
            # A channel may wrap the fields under "values" or send them flat.
            nested = answer.get("values")
            values: dict[str, Any] = nested if isinstance(nested, dict) else answer
            return FormOut(
                values={
                    k: v
                    for k, v in values.items()
                    if k not in ("responder", ATTESTED_KEY)
                },
                responder=responder_of(answer),
            )
        return FormOut(values={"value": answer})


# ---------------------------------------------------------------------------
# human.review_edit
# ---------------------------------------------------------------------------


class ReviewIn(BaseModel):
    subject: str = Field(description="Identifies this review within the run.")
    draft: Any = Field(default=None, description="What the person is reviewing.")
    prompt: str = Field(default="", description="What to look for.")
    assignees: list[str] = Field(default_factory=list)
    timeout: NodeDuration | None = None
    approve_on_timeout: bool = Field(
        default=False, description="Ship the draft unedited if nobody looks."
    )


class ReviewOut(BaseModel):
    content: Any = None
    edited: bool = False
    approved: bool = True
    responder: str = ""
    comment: str = ""
    timed_out: bool = False


@register_node
class ReviewNode(Node[ReviewIn, ReviewOut]):
    """Show a person a draft and let them edit it before it is used."""

    spec = human_spec(
        id="human.review_edit",
        open_world=True,
        summary="Show a draft for review; the person may edit it before it is used.",
        description=(
            "`edited` reports whether the returned content differs from the "
            "draft, so a workflow can log the change without diffing."
        ),
        tags=["review", "edit", "draft", "hitl"],
        examples=[
            NodeExample(
                payload={
                    "subject": "reply-draft",
                    "prompt": "Check tone and facts before this goes out.",
                    "draft": "Hi Dana — thanks for flagging that...",
                }
            )
        ],
    )
    Input, Output = ReviewIn, ReviewOut

    async def run(self, ctx: NodeContext, payload: ReviewIn) -> ReviewOut:
        answer, timed_out, _ = await ask(
            ctx,
            node_id=self.spec.id,
            subject=payload.subject,
            prompt=payload.prompt or "Review this before it is used:",
            context={"draft": payload.draft},
            assignees=payload.assignees,
            response_schema=ReviewOut.model_json_schema(),
            timeout=payload.timeout,
        )
        if timed_out:
            return ReviewOut(
                content=payload.draft,
                approved=payload.approve_on_timeout,
                timed_out=True,
                comment="nobody reviewed it in time",
            )

        if not isinstance(answer, dict):
            content = answer
            return ReviewOut(content=content, edited=content != payload.draft)

        content = answer.get("content", payload.draft)
        approved = bool(answer.get("approved", True))
        if not approved and answer.get("raise_on_reject"):
            raise ApprovalRejected(f"{payload.subject} was rejected in review")
        return ReviewOut(
            content=content,
            edited=content != payload.draft,
            approved=approved,
            responder=responder_of(answer),
            comment=str(answer.get("comment", "")),
        )


# ---------------------------------------------------------------------------
# human.escalate
# ---------------------------------------------------------------------------


class EscalateIn(BaseModel):
    subject: str = Field(description="Identifies this decision within the run.")
    prompt: str = Field(default="", description="What is being asked.")
    tiers: list[list[str]] = Field(
        default_factory=list,
        description="Assignees per tier, tried in order until somebody answers.",
    )
    tier_timeout: NodeDuration = Field(
        default=3600.0, description="Seconds each tier gets before the next is asked."
    )
    context: dict[str, Any] = Field(default_factory=dict)
    on_exhausted: str = Field(
        default=TimeoutPolicy.REJECT,
        description="reject | approve | fail — what it means when no tier answered.",
    )


class EscalateOut(BaseModel):
    approved: bool = False
    responder: str = ""
    tier_reached: int = -1
    """Which tier answered, zero-based. ``-1`` means nobody did."""
    timed_out: bool = False


@register_node
class EscalateNode(Node[EscalateIn, EscalateOut]):
    """Ask each tier in turn until somebody answers."""

    spec = human_spec(
        id="human.escalate",
        open_world=True,
        summary="Ask each tier of assignees in turn until somebody answers.",
        description=(
            "Each tier is a separate request with its own subject, so an answer "
            "from tier 0 arriving late cannot resolve tier 1."
        ),
        tags=["escalate", "oncall", "approval", "hitl"],
        examples=[
            NodeExample(
                payload={
                    "subject": "prod-deploy",
                    "prompt": "Approve the production deploy?",
                    "tiers": [["oncall@acme.com"], ["lead@acme.com"], ["cto@acme.com"]],
                    "tier_timeout": 1800,
                }
            )
        ],
    )
    Input, Output = EscalateIn, EscalateOut

    async def run(self, ctx: NodeContext, payload: EscalateIn) -> EscalateOut:
        for tier, assignees in enumerate(payload.tiers):
            # A distinct subject per tier: sharing one would let a late answer
            # from an earlier tier resolve a request nobody asked them about.
            answer, timed_out, _ = await ask(
                ctx,
                node_id=self.spec.id,
                subject=f"{payload.subject}#{tier}",
                prompt=payload.prompt or f"Approve {payload.subject}?",
                context={**payload.context, "tier": tier, "escalated": tier > 0},
                assignees=assignees,
                response_schema=EscalateOut.model_json_schema(),
                timeout=payload.tier_timeout,
            )
            if timed_out:
                continue
            decision = _approval_from(answer, decided_at=ctx.now())
            return EscalateOut(
                approved=decision.approved,
                responder=decision.responder,
                tier_reached=tier,
            )

        if payload.on_exhausted == TimeoutPolicy.FAIL:
            raise HumanRequestExpired(
                f"no tier answered {payload.subject!r} and on_exhausted='fail'"
            )
        return EscalateOut(
            approved=payload.on_exhausted == TimeoutPolicy.APPROVE, timed_out=True
        )
