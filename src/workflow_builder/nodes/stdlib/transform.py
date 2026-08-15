"""``transform.*`` — pure data work.

All deterministic, all free to recompute. These are the shapes that otherwise
appear as a lambda in a workflow body, where they are invisible in the graph and
untestable on their own.
"""

from __future__ import annotations

import re
from string import Template
from typing import Any

from pydantic import BaseModel, Field

from workflow_builder.nodes.base import Node, NodeContext
from workflow_builder.nodes.registry import register_node
from workflow_builder.nodes.spec import NodeCategory, NodeExample, NodeSpec

__all__ = ["ExtractNode", "JoinNode", "MapFieldsNode", "RedactNode", "TemplateNode"]

def transform_spec(**declared: Any) -> NodeSpec:
    """A ``transform.*`` spec. All pure data work, so deterministic."""
    return NodeSpec(
        import_module="workflow_builder.nodes.stdlib.transform",
        category=NodeCategory.TRANSFORM,
        deterministic=True,
        **declared,
    )


def _read(row: Any, path: str) -> Any:
    current = row
    for part in path.split("."):
        current = current.get(part) if isinstance(current, dict) else getattr(current, part, None)
        if current is None:
            return None
    return current


# ---------------------------------------------------------------------------


class MapFieldsIn(BaseModel):
    items: list[Any] = Field(default_factory=list, description="Rows to reshape.")
    mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Output field name to the dotted source path, e.g. {'email': 'user.email'}.",
    )
    keep_unmapped: bool = Field(
        default=False, description="Carry through fields not named in the mapping."
    )


class MapFieldsOut(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    missing: dict[str, int] = Field(default_factory=dict)
    """How many rows had no value for each mapped field. A silent ``None`` in
    every row is the usual sign that a source path is wrong, and it is invisible
    unless somebody counts."""


@register_node
class MapFieldsNode(Node[MapFieldsIn, MapFieldsOut]):
    """Reshape rows by renaming and picking fields."""

    spec = transform_spec(
        id="transform.map_fields",
        summary="Reshape rows by renaming and picking fields, reporting misses.",
        tags=["map", "rename", "pick", "reshape"],
        examples=[
            NodeExample(
                payload={
                    "items": [{"user": {"email": "a@b.com"}, "n": 1}],
                    "mapping": {"email": "user.email", "count": "n"},
                }
            )
        ],
    )
    Input, Output = MapFieldsIn, MapFieldsOut

    async def run(self, ctx: NodeContext, payload: MapFieldsIn) -> MapFieldsOut:
        rows: list[dict[str, Any]] = []
        missing: dict[str, int] = {}
        for row in payload.items:
            shaped: dict[str, Any] = {}
            if payload.keep_unmapped and isinstance(row, dict):
                shaped.update(row)
            for target, source in payload.mapping.items():
                value = _read(row, source)
                if value is None:
                    missing[target] = missing.get(target, 0) + 1
                shaped[target] = value
            rows.append(shaped)
        return MapFieldsOut(items=rows, missing=missing)


# ---------------------------------------------------------------------------


class TemplateIn(BaseModel):
    template: str = Field(
        default="", description="A $-substitution template, e.g. 'Hi $name'."
    )
    values: dict[str, Any] = Field(default_factory=dict, description="Substitutions.")
    strict: bool = Field(
        default=True, description="Fail on a placeholder with no value."
    )


class TemplateOut(BaseModel):
    text: str = ""
    unresolved: list[str] = Field(default_factory=list)


@register_node
class TemplateNode(Node[TemplateIn, TemplateOut]):
    """Fill a text template from a mapping."""

    spec = transform_spec(
        id="transform.template",
        summary="Fill a $-substitution text template from a mapping.",
        description=(
            "strict=True raises on a missing placeholder. The alternative — "
            "rendering '$name' into the message that goes to a customer — is the "
            "failure this node exists to make loud."
        ),
        tags=["template", "format", "render", "text"],
        examples=[
            NodeExample(
                payload={"template": "Hi $name, your order $id shipped.",
                         "values": {"name": "Dana", "id": "4821"}}
            )
        ],
    )
    Input, Output = TemplateIn, TemplateOut

    async def run(self, ctx: NodeContext, payload: TemplateIn) -> TemplateOut:
        template = Template(payload.template)
        if payload.strict:
            return TemplateOut(text=template.substitute(payload.values))
        text = template.safe_substitute(payload.values)
        unresolved = sorted(set(re.findall(r"\$\{?(\w+)\}?", text)))
        return TemplateOut(text=text, unresolved=unresolved)


# ---------------------------------------------------------------------------


class ExtractIn(BaseModel):
    text: str = Field(default="", description="What to search.")
    pattern: str = Field(default="", description="A regular expression.")
    group: int = Field(default=0, description="Which capture group to return.")
    all: bool = Field(default=False, description="Return every match, not the first.")


class ExtractOut(BaseModel):
    matches: list[str] = Field(default_factory=list)
    found: bool = False


@register_node
class ExtractNode(Node[ExtractIn, ExtractOut]):
    """Pull substrings out of text with a regular expression."""

    spec = transform_spec(
        id="transform.extract",
        summary="Pull substrings out of text with a regular expression.",
        description=(
            "For text with a known shape — an order id, a URL. Extracting meaning "
            "from prose is judgement: use agent.extract_structured."
        ),
        tags=["extract", "regex", "parse"],
        examples=[
            NodeExample(payload={"text": "order 4821 shipped",
                                 "pattern": r"order (\d+)", "group": 1})
        ],
    )
    Input, Output = ExtractIn, ExtractOut

    async def run(self, ctx: NodeContext, payload: ExtractIn) -> ExtractOut:
        if not payload.pattern:
            return ExtractOut()
        compiled = re.compile(payload.pattern)
        if payload.all:
            found = [m.group(payload.group) for m in compiled.finditer(payload.text)]
            return ExtractOut(matches=found, found=bool(found))
        match = compiled.search(payload.text)
        return ExtractOut(
            matches=[match.group(payload.group)] if match else [], found=match is not None
        )


# ---------------------------------------------------------------------------


class JoinIn(BaseModel):
    left: list[Any] = Field(default_factory=list)
    right: list[Any] = Field(default_factory=list)
    left_key: str = Field(default="id", description="Dotted path on the left rows.")
    right_key: str = Field(default="id", description="Dotted path on the right rows.")
    how: str = Field(default="inner", description="inner | left")


class JoinOut(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    unmatched_left: int = 0


@register_node
class JoinNode(Node[JoinIn, JoinOut]):
    """Join two lists of rows on a key."""

    spec = transform_spec(
        id="transform.join",
        summary="Join two lists of rows on a key, reporting unmatched left rows.",
        tags=["join", "merge", "lookup"],
        examples=[
            NodeExample(
                payload={
                    "left": [{"id": 1, "n": "a"}],
                    "right": [{"id": 1, "email": "a@b.com"}],
                    "left_key": "id",
                    "right_key": "id",
                }
            )
        ],
    )
    Input, Output = JoinIn, JoinOut

    async def run(self, ctx: NodeContext, payload: JoinIn) -> JoinOut:
        index: dict[Any, Any] = {}
        for row in payload.right:
            index[_read(row, payload.right_key)] = row

        rows: list[dict[str, Any]] = []
        unmatched = 0
        for row in payload.left:
            match = index.get(_read(row, payload.left_key))
            if match is None:
                unmatched += 1
                if payload.how != "left":
                    continue
            merged = dict(row) if isinstance(row, dict) else {"left": row}
            if isinstance(match, dict):
                merged.update(match)
            elif match is not None:
                merged["right"] = match
            rows.append(merged)
        return JoinOut(items=rows, unmatched_left=unmatched)


# ---------------------------------------------------------------------------


class RedactIn(BaseModel):
    text: str = Field(default="", description="What to redact.")
    patterns: list[str] = Field(
        default_factory=list, description="Regular expressions to replace."
    )
    replacement: str = Field(default="[redacted]", description="What to put in place.")


class RedactOut(BaseModel):
    text: str = ""
    redactions: int = 0


@register_node
class RedactNode(Node[RedactIn, RedactOut]):
    """Replace matches with a placeholder, and say how many."""

    spec = transform_spec(
        id="transform.redact",
        summary="Replace regex matches in text with a placeholder.",
        description=(
            "For patterns you name. guard.pii carries the detectors and can "
            "reject rather than only rewrite."
        ),
        tags=["redact", "mask", "scrub"],
        examples=[
            NodeExample(payload={"text": "call 555-123-4567",
                                 "patterns": [r"\d{3}-\d{3}-\d{4}"]})
        ],
    )
    Input, Output = RedactIn, RedactOut

    async def run(self, ctx: NodeContext, payload: RedactIn) -> RedactOut:
        text = payload.text
        count = 0
        for pattern in payload.patterns:
            text, n = re.subn(pattern, payload.replacement, text)
            count += n
        return RedactOut(text=text, redactions=count)
