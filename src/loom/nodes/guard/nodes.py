"""The built-in ``guard.*`` nodes.

Each returns a :class:`GuardVerdict` and nothing else — the decision about what
a verdict *does* lives in :mod:`loom.nodes.guard.runner`, so a guard
cannot accidentally decide policy by raising the wrong thing.

Note ``deterministic`` on each spec. It is not decoration: a guard whose verdict
could differ on replay must have that verdict journaled, or a replay would
re-decide against a policy that has since changed and disagree with history.
``guard.content`` is the one that is false.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from loom.nodes.base import Node, NodeContext
from loom.nodes.guard.runner import GuardVerdict
from loom.nodes.registry import register_node
from loom.nodes.spec import NodeCategory, NodeExample, NodeSpec

__all__ = [
    "BudgetGuard",
    "BudgetIn",
    "ContentGuard",
    "ContentIn",
    "PiiGuard",
    "PiiIn",
    "PolicyGuard",
    "PolicyIn",
    "SchemaGuard",
    "SchemaIn",
]

def guard_spec(**declared: Any) -> NodeSpec:
    """A ``guard.*`` spec. Typed factory, not a splatted dict — see human_spec."""
    return NodeSpec(
        import_module="loom.nodes.guard",
        category=NodeCategory.GUARD,
        **declared,
    )


# ---------------------------------------------------------------------------
# guard.schema
# ---------------------------------------------------------------------------


class SchemaIn(BaseModel):
    value: Any = Field(default=None, description="What is being checked.")
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        alias="expect",
        description="JSON Schema the value must satisfy.",
    )
    model_config = {"populate_by_name": True}


@register_node
class SchemaGuard(Node[SchemaIn, GuardVerdict]):
    """Reject a value that does not satisfy a JSON Schema."""

    spec = guard_spec(
        id="guard.schema",
        summary="Reject a value that does not satisfy a JSON Schema.",
        tags=["schema", "validate", "shape"],
        deterministic=True,
        examples=[
            NodeExample(
                payload={
                    "value": {"email": "a@b.com"},
                    "expect": {"type": "object", "required": ["email"]},
                }
            )
        ],
    )
    Input, Output = SchemaIn, GuardVerdict

    async def run(self, ctx: NodeContext, payload: SchemaIn) -> GuardVerdict:
        if not payload.schema_:
            return GuardVerdict.allow()
        try:
            import jsonschema  # type: ignore[import-untyped]
        except ImportError:
            return self._structural(payload)
        try:
            jsonschema.validate(payload.value, payload.schema_)
        except Exception as exc:
            return GuardVerdict.reject(f"does not match the declared schema: {exc}")
        return GuardVerdict.allow()

    @staticmethod
    def _structural(payload: SchemaIn) -> GuardVerdict:
        """Required keys and top-level type, when ``jsonschema`` is not installed.

        Deliberately narrow, and it says so in the message. A guard that
        silently checked less than it claimed would be worse than one that is
        absent, because the caller would believe the value was validated.
        """
        expected = str(payload.schema_.get("type", ""))
        kinds: dict[str, type | tuple[type, ...]] = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
        }
        if expected in kinds and not isinstance(payload.value, kinds[expected]):
            return GuardVerdict.reject(
                f"expected {expected}, got {type(payload.value).__name__}"
            )
        missing = [
            key
            for key in payload.schema_.get("required", [])
            if not isinstance(payload.value, dict) or key not in payload.value
        ]
        if missing:
            return GuardVerdict.reject(f"missing required field(s): {', '.join(missing)}")
        return GuardVerdict.allow(
            info="checked structurally only — `pip install jsonschema` for the full check"
        )


# ---------------------------------------------------------------------------
# guard.policy
# ---------------------------------------------------------------------------


class PolicyIn(BaseModel):
    value: Any = Field(default=None, description="What is being checked.")
    deny_if: list[str] = Field(
        default_factory=list,
        description="Substrings that, if present, reject the value.",
    )
    require_all: list[str] = Field(
        default_factory=list,
        description="Substrings that must all be present.",
    )
    message: str = Field(default="", description="Why the policy exists.")


@register_node
class PolicyGuard(Node[PolicyIn, GuardVerdict]):
    """Reject a value on a declared substring policy."""

    spec = guard_spec(
        id="guard.policy",
        summary="Reject a value that contains, or is missing, declared markers.",
        description=(
            "For rules you can state exactly. When the rule needs judgement — "
            "'is this rude', 'is this off-topic' — use guard.content, which asks "
            "a model and journals its verdict."
        ),
        tags=["policy", "deny", "allow", "rules"],
        deterministic=True,
        examples=[
            NodeExample(payload={"value": "...", "deny_if": ["DROP TABLE", "rm -rf"]})
        ],
    )
    Input, Output = PolicyIn, GuardVerdict

    async def run(self, ctx: NodeContext, payload: PolicyIn) -> GuardVerdict:
        text = str(payload.value)
        for marker in payload.deny_if:
            if marker in text:
                return GuardVerdict.reject(payload.message or f"contains {marker!r}")
        missing = [m for m in payload.require_all if m not in text]
        if missing:
            return GuardVerdict.reject(
                payload.message or f"missing required marker(s): {', '.join(missing)}"
            )
        return GuardVerdict.allow()


# ---------------------------------------------------------------------------
# guard.pii
# ---------------------------------------------------------------------------

#: Narrow on purpose. Each pattern matches a format that is unambiguous when it
#: matches; a looser set would redact ordinary numbers and a workflow author
#: would turn the guard off, which is worse than a guard that catches less.
_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "api_key": re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "phone": re.compile(r"\b\+?\d{1,3}[ -]?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}\b"),
}


class PiiIn(BaseModel):
    value: Any = Field(default=None, description="What is being checked.")
    kinds: list[str] = Field(
        default_factory=lambda: ["credit_card", "ssn", "api_key", "aws_key"],
        description=f"Which detectors to run. Available: {', '.join(_PII_PATTERNS)}.",
    )
    redact: bool = Field(
        default=False,
        description="Replace matches and continue, instead of rejecting.",
    )


@register_node
class PiiGuard(Node[PiiIn, GuardVerdict]):
    """Detect secrets and personal data, and reject or redact them."""

    spec = guard_spec(
        id="guard.pii",
        summary="Detect secrets and personal data; reject or redact.",
        description=(
            "`email` and `phone` are off by default: most workflows legitimately "
            "carry them, and a guard that fires constantly gets removed."
        ),
        tags=["pii", "secrets", "redact", "privacy"],
        deterministic=True,
        examples=[NodeExample(payload={"value": "...", "redact": True})],
    )
    Input, Output = PiiIn, GuardVerdict

    async def run(self, ctx: NodeContext, payload: PiiIn) -> GuardVerdict:
        text = str(payload.value)
        found: dict[str, int] = {}
        redacted = text
        for kind in payload.kinds:
            pattern = _PII_PATTERNS.get(kind)
            if pattern is None:
                continue
            matches = pattern.findall(text)
            if matches:
                found[kind] = len(matches)
                redacted = pattern.sub(f"[redacted:{kind}]", redacted)
        if not found:
            return GuardVerdict.allow()
        summary = ", ".join(f"{n}x {k}" for k, n in found.items())
        if payload.redact:
            return GuardVerdict.replace_with(redacted, message=f"redacted {summary}")
        return GuardVerdict.reject(f"contains {summary}", info=found)


# ---------------------------------------------------------------------------
# guard.budget
# ---------------------------------------------------------------------------


class BudgetIn(BaseModel):
    value: Any = Field(default=None, description="Passed through when within budget.")
    max_cost: float | None = Field(
        default=None, description="Cost ceiling for the run, in USD."
    )
    max_tokens: int | None = Field(
        default=None, description="Total token ceiling for the run."
    )
    tripwire: bool = Field(
        default=False, description="Abort the run rather than refusing this call."
    )


@register_node
class BudgetGuard(Node[BudgetIn, GuardVerdict]):
    """Stop when the run has spent its budget."""

    spec = guard_spec(
        id="guard.budget",
        summary="Refuse further work once the run has spent its token or cost budget.",
        description=(
            "Reads ctx.usage, which the journal accumulates, so it measures the "
            "run rather than the process."
        ),
        tags=["budget", "cost", "tokens", "limit"],
        deterministic=False,
        examples=[NodeExample(payload={"max_cost": 1.5, "tripwire": True})],
    )
    Input, Output = BudgetIn, GuardVerdict

    async def run(self, ctx: NodeContext, payload: BudgetIn) -> GuardVerdict:
        usage = ctx.usage
        cost = float(getattr(usage, "cost", 0.0) or 0.0)
        tokens = int(getattr(usage, "total_tokens", 0) or 0)

        over: list[str] = []
        if payload.max_cost is not None and cost > payload.max_cost:
            over.append(f"cost ${cost:.4f} > ${payload.max_cost:.4f}")
        if payload.max_tokens is not None and tokens > payload.max_tokens:
            over.append(f"{tokens} tokens > {payload.max_tokens}")
        if not over:
            return GuardVerdict.allow(info={"cost": cost, "tokens": tokens})

        message = "budget exhausted: " + "; ".join(over)
        return (
            GuardVerdict.tripwire(message)
            if payload.tripwire
            else GuardVerdict.reject(message)
        )


# ---------------------------------------------------------------------------
# guard.content
# ---------------------------------------------------------------------------


class ContentIn(BaseModel):
    value: Any = Field(default=None, description="What is being judged.")
    rule: str = Field(
        default="",
        description="The rule in plain language, e.g. 'no medical advice'.",
    )
    tripwire: bool = Field(
        default=False, description="Abort the run rather than refusing this call."
    )


@register_node
class ContentGuard(Node[ContentIn, GuardVerdict]):
    """Ask a model whether content satisfies a rule stated in plain language."""

    spec = guard_spec(
        id="guard.content",
        summary="Judge content against a plain-language rule, using a model.",
        description=(
            "For rules that cannot be written as a condition today. If you can "
            "state the rule exactly, use guard.policy — it is free, and it does "
            "not put a nondeterministic call in every run."
        ),
        tags=["content", "moderation", "judgement", "llm"],
        deterministic=False,
        examples=[
            NodeExample(payload={"value": "...", "rule": "no medical advice"})
        ],
    )
    Input, Output = ContentIn, GuardVerdict

    async def run(self, ctx: NodeContext, payload: ContentIn) -> GuardVerdict:
        if not payload.rule:
            return GuardVerdict.allow(info="no rule given")
        answer = await ctx.agent(
            "You are a content guard. Answer with exactly one word, ALLOW or "
            f"BLOCK.\n\nRule: {payload.rule}\n\nContent:\n{payload.value}"
        )
        text = str(getattr(answer, "output", answer) or "").strip().upper()
        if text.startswith("BLOCK"):
            message = f"content violates the rule: {payload.rule}"
            return (
                GuardVerdict.tripwire(message)
                if payload.tripwire
                else GuardVerdict.reject(message)
            )
        if not text.startswith("ALLOW"):
            # An unreadable verdict is not an allow. A guard that cannot decide
            # has found nothing, which is not the same as finding nothing wrong.
            return GuardVerdict.reject(
                f"the content guard returned {text[:40]!r}, which is neither "
                "ALLOW nor BLOCK"
            )
        return GuardVerdict.allow()
