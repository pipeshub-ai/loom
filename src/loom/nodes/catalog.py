"""Three tiers of node information, for a catalog that stays cheap as it grows.

Same shape as :class:`ToolsetCatalog`, with one deliberate difference that is
the whole point of the tier-3 method.

``ToolsetCatalog.stub()`` returns JSON Schema — a *description of* a call. The
agent's next action is to *write* a call, so it still has to translate schema
into Python, and every translation is a chance to invent a keyword argument or
drop a required field. :meth:`NodeCatalog.contract` returns the invocation
itself: the exact import line, the call, the typed annotation, whether it parks
the run, and what the Runtime must have configured.

The other difference is growth. ``describe(detail="index")`` on the toolset side
enumerates operation names, so it costs ~830 characters per toolset — fine at
four, 21k tokens at a hundred. :meth:`prompt_block` here is O(categories):
registering the five-hundredth node adds nothing to any prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from loom.nodes.base import Node, derive_spec, near_matches
from loom.nodes.errors import NodeNotFound
from loom.nodes.spec import (
    CATEGORY_BLURBS,
    EffectClass,
    NodeCategory,
    NodeSpec,
)

__all__ = ["NodeCard", "NodeCatalog", "NodeDetail"]


# ---------------------------------------------------------------------------
# Tier 1 — search results
# ---------------------------------------------------------------------------


class NodeCard(BaseModel):
    """One search hit. Small on purpose — the agent gets many of these."""

    id: str
    category: NodeCategory
    summary: str
    suspends: bool = False
    requires: list[str] = Field(default_factory=list)


class NodeDetail(BaseModel):
    """Everything about one node except the rendered call."""

    id: str
    version: str
    category: NodeCategory
    summary: str
    description: str = ""
    effect: EffectClass = EffectClass.READ
    suspends: bool = False
    deterministic: bool = True
    requires: list[str] = Field(default_factory=list)
    guards: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    import_line: str = ""


# ---------------------------------------------------------------------------
# Rendering a call from a schema
# ---------------------------------------------------------------------------


def _type_name(schema: dict[str, Any]) -> str:
    """A Python-looking name for a JSON Schema fragment.

    Best effort by design: this is a comment in generated code, so an imperfect
    name costs a little clarity, while refusing to render one costs the agent
    the type entirely.
    """
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "anyOf" in schema:
        parts = [_type_name(s) for s in schema["anyOf"]]
        return " | ".join(dict.fromkeys(parts))
    if "enum" in schema:
        return " | ".join(repr(v) for v in schema["enum"])
    # `format` is where Pydantic puts the type JSON Schema cannot express. Without
    # it a timedelta renders as `str` and a datetime as `str`, and the agent
    # writes a string where the model wants an object.
    by_format = {"duration": "timedelta", "date-time": "datetime", "date": "date",
                 "time": "time", "uuid": "UUID", "binary": "bytes"}
    if (fmt := schema.get("format")) in by_format:
        return by_format[str(fmt)]
    kind = schema.get("type")
    if kind == "array":
        return f"list[{_type_name(schema.get('items', {}))}]"
    if kind == "object":
        return "dict"
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }.get(str(kind), "Any")


def _placeholder(schema: dict[str, Any]) -> Any:
    """A stand-in value when neither an example nor a default supplies one."""
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return {
        "string": "...",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
    }.get(str(kind))


def render_call(spec: NodeSpec) -> str:
    """The code to write, not a description of it.

    Rendered from the node's own schemas, so it cannot drift from the class.
    ``examples[0]`` supplies the argument values when the node ships one; the
    field's default or a type-appropriate placeholder when it does not.
    """
    schema = spec.input_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    example = spec.examples[0].payload if spec.examples else {}

    in_name = schema.get("title") or "Input"
    out_name = spec.output_schema.get("title") or "Output"

    header = (
        f"# {spec.id}  v{spec.version}  [{spec.category.value}]"
        f"   suspends: {'yes' if spec.suspends else 'no'}"
        f"   effect: {spec.effect.value}"
    )
    if spec.requires:
        header += f"   requires: {', '.join(spec.requires)}"

    lines = [header, ""]
    if line := spec.import_line():
        lines += [line, ""]

    lines.append(f"result: {out_name} = await ctx.node(")
    lines.append(f"    {spec.id!r},")
    if not properties:
        lines.append(f"    {in_name}(),")
    else:
        lines.append(f"    {in_name}(")
        rendered = [
            (_assignment(name, value), name, value)
            for name, value in _values(properties, example)
        ]
        width = max(len(a) for a, _, _ in rendered)
        for assignment, field_name, _value in rendered:
            field_schema = properties[field_name]
            note = _type_name(field_schema)
            if field_name not in required:
                note += ", optional"
            if description := field_schema.get("description", ""):
                note += f" — {description}"
            lines.append(f"        {assignment.ljust(width)}  # {note}")
        lines.append("    ),")
    lines.append(")")

    out_fields = spec.output_schema.get("properties", {})
    if out_fields:
        signature = ", ".join(f"{n}: {_type_name(s)}" for n, s in out_fields.items())
        lines += ["", f"# {out_name}: {signature}"]

    if spec.description:
        lines += ["", *(f"# {line}" for line in spec.description.strip().splitlines())]
    if spec.examples and spec.examples[0].notes:
        lines += ["", *(f"# {line}" for line in spec.examples[0].notes.splitlines())]
    if spec.suspends:
        lines += [
            "",
            "# Parks the run. It costs nothing while parked, and resumes when the",
            "# answer arrives — see `loom pending` for what is waiting.",
        ]
    return "\n".join(lines)


def _values(
    properties: dict[str, Any], example: dict[str, Any]
) -> list[tuple[str, Any]]:
    """Field name and the value to render, example first, then default."""
    out: list[tuple[str, Any]] = []
    for name, field_schema in properties.items():
        if name in example:
            out.append((name, example[name]))
        elif "default" in field_schema:
            out.append((name, field_schema["default"]))
        else:
            out.append((name, _placeholder(field_schema)))
    return out


def _literal(value: Any) -> str:
    return repr(value)


def _assignment(name: str, value: Any) -> str:
    """``name=value,`` — or the keyword-safe form when the name is not an identifier.

    A field aliased to ``in``, ``class``, or ``from`` cannot be written as a
    keyword argument, and rendering one anyway hands the agent code that does
    not parse. Found by the test that compiles every rendered contract, on
    ``control.filter``, whose ``in`` alias was fixed at the source — this is the
    backstop for third-party nodes doing the same thing.
    """
    if name.isidentifier() and not _is_keyword(name):
        return f"{name}={_literal(value)},"
    return f"**{{{name!r}: {_literal(value)}}},"


def _is_keyword(name: str) -> bool:
    import keyword

    return keyword.iskeyword(name)


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


class NodeCatalog:
    """Serves node information at three tiers.

    Layer 1 holds :class:`NodeSpec` only — pure data, no node module imported.
    A subprocess probe in the test suite asserts that, because the property is
    invisible in-process once anything else has imported the module.
    """

    def __init__(self) -> None:
        self._specs: dict[str, NodeSpec] = {}

    # -- registration -------------------------------------------------------

    def register(self, spec: NodeSpec, /) -> None:
        """Register a spec without importing its code."""
        self._specs[spec.id] = spec

    def register_node(self, cls: type[Node[Any, Any]], /) -> NodeSpec:
        """Register a node class, deriving everything derivable from it."""
        spec = derive_spec(cls)
        self._specs[spec.id] = spec
        return spec

    def unregister(self, node_id: str) -> None:
        self._specs.pop(node_id, None)

    def get(self, node_id: str) -> NodeSpec | None:
        return self._specs.get(node_id)

    def require(self, node_id: str) -> NodeSpec:
        """:meth:`get`, but a miss names the near matches instead of returning None."""
        spec = self.get(node_id)
        if spec is None:
            raise NodeNotFound(node_id, suggestions=near_matches(node_id, self.node_ids()))
        return spec

    def node_ids(self) -> list[str]:
        return sorted(self._specs)

    # -- Tier 1 -------------------------------------------------------------

    def categories(self) -> dict[NodeCategory, int]:
        """How many nodes sit in each category. What the prompt block reports."""
        counts: dict[NodeCategory, int] = {}
        for spec in self._specs.values():
            counts[spec.category] = counts.get(spec.category, 0) + 1
        return counts

    def search(
        self,
        query: str = "",
        *,
        category: NodeCategory | str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[NodeCard]:
        """Find nodes. An empty query with a category lists that category.

        That affordance is why the categorised catalog is worth having: "I need
        a human decision" is answered by ``category="human"`` without having to
        guess a keyword, and a model with no keyword otherwise has nowhere to
        start.
        """
        wanted = NodeCategory(category) if category is not None else None
        terms = query.lower().split()
        wanted_tags = {t.lower() for t in (tags or [])}

        scored: list[tuple[int, str, NodeCard]] = []
        for spec in self._specs.values():
            if wanted is not None and spec.category is not wanted:
                continue
            if wanted_tags and not wanted_tags <= {t.lower() for t in spec.tags}:
                continue
            score = self._score(terms, spec)
            if terms and score <= 0:
                continue
            scored.append((score, spec.id, self._card(spec)))

        scored.sort(key=lambda row: (-row[0], row[1]))
        return [card for _, _, card in scored[:limit]]

    @staticmethod
    def _card(spec: NodeSpec) -> NodeCard:
        return NodeCard(
            id=spec.id,
            category=spec.category,
            summary=spec.summary,
            suspends=spec.suspends,
            requires=list(spec.requires),
        )

    @staticmethod
    def _score(terms: list[str], spec: NodeSpec) -> int:
        if not terms:
            return 1
        haystacks = (
            (spec.id.lower(), 3),
            (spec.summary.lower(), 2),
            (" ".join(spec.tags).lower(), 2),
            (spec.description.lower(), 1),
            (spec.category.value, 1),
        )
        return sum(
            weight for term in terms for text, weight in haystacks if term in text
        )

    # -- Tier 2 -------------------------------------------------------------

    def show(self, node_id: str) -> NodeDetail:
        spec = self.require(node_id)
        return NodeDetail(
            id=spec.id,
            version=spec.version,
            category=spec.category,
            summary=spec.summary,
            description=spec.description,
            effect=spec.effect,
            suspends=spec.suspends,
            deterministic=spec.deterministic,
            requires=list(spec.requires),
            guards=list(spec.guards),
            tags=list(spec.tags),
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            examples=[e.model_dump() for e in spec.examples],
            import_line=spec.import_line(),
        )

    # -- Tier 3 -------------------------------------------------------------

    def contract(self, node_id: str) -> str:
        """The code to write for this node."""
        return render_call(self.require(node_id))

    # -- the prompt block ---------------------------------------------------

    def prompt_block(self) -> str:
        """Category headers and counts. **O(categories), never O(nodes).**

        This is the one hard budget in the node design. Detail arrives through
        ``search_nodes``/``show_node``/``node_contract`` on demand; nothing here
        enumerates the catalog, so a project registering five hundred custom
        nodes does not lengthen a single prompt. ``test_node_agent_tools``
        asserts that as exact equality rather than a tolerance — a tolerance is
        a budget that erodes.
        """
        counts = self.categories()
        if not counts:
            return ""
        lines = [
            "## Node catalog",
            "",
            "Reusable typed units: Pydantic in, Pydantic out, called with",
            "`await ctx.node(\"<id>\", <Input>(...))`. Prefer one over hand-written",
            "code when a catalogued contract already covers the work.",
            "",
        ]
        for category in NodeCategory:
            count = counts.get(category, 0)
            if not count:
                continue
            noun = "node " if count == 1 else "nodes"
            lines.append(
                f"  {category.value:<10} {count:>3} {noun}   {CATEGORY_BLURBS[category]}"
            )
        lines += [
            "",
            'search_nodes(query, category=...) to find one — an empty query with a',
            "category lists it. node_contract(id) for the exact code to write.",
        ]
        return "\n".join(lines)
