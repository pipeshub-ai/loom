"""What a node declares about itself.

:class:`NodeSpec` is pure data — a Pydantic model with no imports of node code —
so a catalog can hold a thousand of them without importing a single node module.
That separation is the whole reason the catalog scales, and it is asserted by a
subprocess probe rather than trusted.

Everything the coding agent sees is *derived* from the node class:
``input_schema`` and ``output_schema`` from the two models, ``node_class`` from
the class itself. Nothing is authored twice. Toolsets learned this the hard way
— the client paged, the tool returned ``Results``, and the manifest said
``pagination: False``, three sources of truth for one fact.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Any, TypeAlias

from pydantic import BaseModel, Field

from loom.core.ids import stable_hash
from loom.toolsets.manifest import EffectClass

#: What a node model declares for a timeout.
#:
#: ``core.types.Duration`` is a *string* TypeAlias ("float | int | timedelta"),
#: which Pydantic cannot evaluate under ``from __future__ import annotations`` —
#: ``Duration | None`` raises ``unsupported operand type(s) for |: 'str' and
#: 'NoneType'`` at class construction. This is the same union as a real type, so
#: the models build and the generated JSON Schema is accurate.
#:
#: Seconds or a ``timedelta``. Not a string: ``to_seconds`` does not parse "24h",
#: so an example promising one would hand the coding agent code that fails.
NodeDuration: TypeAlias = float | int | timedelta

__all__ = [
    "CATEGORY_BLURBS",
    "EffectClass",
    "NodeCategory",
    "NodeDuration",
    "NodeExample",
    "NodeSpec",
]


class NodeCategory(StrEnum):
    """The searchable axis of the catalog.

    Chosen so that the answer to "which node do I want" *is* a category. In
    particular ``AGENT`` and ``CONTROL`` are separate so that choosing judgement
    over a rule is a deliberate act rather than an accident of what the model
    remembered — the same distinction ``DEFAULT_SYSTEM_PROMPT`` draws in prose.
    """

    HUMAN = "human"
    GUARD = "guard"
    CONTROL = "control"
    TRANSFORM = "transform"
    IO = "io"
    BROWSER = "browser"
    AGENT = "agent"
    CUSTOM = "custom"


#: One line per category for the system prompt. Fixed width by construction: the
#: prompt block is O(categories), never O(nodes), which is the property that lets
#: a project register five hundred custom nodes without lengthening any prompt.
CATEGORY_BLURBS: dict[NodeCategory, str] = {
    NodeCategory.HUMAN: "park the run on a person — approval, choice, form, review",
    NodeCategory.GUARD: "verdict checks — schema, policy, pii, budget, content",
    NodeCategory.CONTROL: "flow shaping — branch, switch, filter, dedupe, batch",
    NodeCategory.TRANSFORM: "pure data work — map_fields, template, extract, redact",
    NodeCategory.IO: "typed external effects — http, webhook_wait, emit",
    NodeCategory.BROWSER: "drive a web page — navigate, snapshot, act, extract",
    NodeCategory.AGENT: "judgement — classify, extract_structured, summarize, judge",
    NodeCategory.CUSTOM: "registered by this project",
}


class NodeExample(BaseModel):
    """A worked call, used three ways.

    It documents the node, it supplies the values :meth:`NodeCatalog.contract`
    renders into a copy-pasteable snippet, and it drives the node's own test. The
    third is what keeps the first two honest: an example that stops working
    fails CI rather than quietly misleading whoever reads it next.
    """

    title: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class NodeSpec(BaseModel):
    """The catalog entry for one node."""

    id: str
    """Dotted and namespaced — ``human.approval``, ``custom.lead_score``."""

    version: str = "1.0.0"
    category: NodeCategory = NodeCategory.CUSTOM
    summary: str = ""
    """One line. This is what ``search_nodes`` returns, so it carries the weight."""

    description: str = ""
    """The long form, shown by ``show_node`` only."""

    effect: EffectClass = EffectClass.READ
    """Reused from toolsets rather than redefined — a node and an operation mean
    the same thing by "this writes", and two enums would drift."""

    effect_by: dict[str, dict[str, EffectClass]] = Field(default_factory=dict)
    """Argument-dependent effect: ``{"method": {"GET": READ, "DELETE": DESTRUCTIVE}}``.

    :attr:`effect` is a property of the operation; for a few it is a property
    of the *call*. ``io.http_request`` is one node with one class, and
    ``method="GET"`` is a read while ``method="DELETE"`` destroys — and it is
    precisely the node a generated workflow reaches for when no toolset covers
    the API.

    Declarative rather than a callable, deliberately: grant validation and the
    catalog read manifest metadata without importing a toolset, and a callable
    would need the module.

    A matched rule wins in **either** direction — ``GET`` lowers the class and
    ``DELETE`` raises it, and both are the author's own declaration about their
    own operation. :attr:`effect` is the fallback, used whenever the argument
    was not passed or its value is not in the table, so an unrecognised method
    keeps the cautious class rather than falling to a read.
    """

    open_world: bool = False
    """Does the body reach outside this process?

    Defaults to ``False``, the opposite of :class:`OperationSpec`, and the
    difference is the point: a toolset operation is a network call by
    definition, while most nodes are computation. ``control.filter`` and
    ``transform.map_fields`` touch nothing; ``io.http_request`` and the
    ``agent.*`` nodes do.

    Read by the taint rule. Before this existed, twenty of the twenty-six
    built-in nodes were classified READ and therefore counted as "this run has
    read external data" — so filtering an in-memory list refused the next
    write, naming ``step:control.filter`` as the external source it had read.
    """

    suspends: bool = False
    """Whether this node can park the run. The agent needs this at the point of
    choosing, not after writing the call, because a suspending node changes what
    the surrounding code may assume."""

    deterministic: bool = True
    """Whether the body can be recomputed on replay without journaling its own
    verdict. A model-backed guard is not; a schema check is."""

    tags: list[str] = Field(default_factory=list)

    node_class: str = ""
    """``module:QualName``. Derived, never authored.

    A catalog entry must say how to import itself. Manifests taught this: without
    ``tools_module`` the docs listed operation ids that exist in no namespace, and
    a model asked to write code against one invents an import to match."""

    import_module: str = ""
    """Where generated code should import the models from.

    Defaults to the module the class lives in. Set it when the public path
    differs — ``loom.nodes.human`` rather than
    ``loom.nodes.human.nodes`` — because the import line is copied
    verbatim into somebody's workflow, and the private path being importable
    today is not a promise."""

    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    requires: list[str] = Field(default_factory=list)
    """Runtime capabilities this node needs, e.g. ``"human_channel"``. Checked at
    resolution so a missing one is a clear error rather than a run that parks
    forever with nobody listening."""

    guards: list[str] = Field(default_factory=list)
    """Guardrail node ids evaluated around this node."""

    examples: list[NodeExample] = Field(default_factory=list)

    @property
    def contract_hash(self) -> str:
        """Stable hash of the two schemas.

        Journalled with each call so that a node upgraded between a run and its
        replay is caught rather than silently decoding an old payload into a new
        model."""
        return stable_hash({"in": self.input_schema, "out": self.output_schema})

    @property
    def namespace(self) -> str:
        """The part before the first dot — ``human`` for ``human.approval``."""
        return self.id.partition(".")[0]

    def import_line(self) -> str:
        """How to import this node's models, or ``""`` when it is not resolvable.

        The models live beside the class, so the module half of ``node_class`` is
        the import path and the two model names come from the schemas' titles.
        """
        if not self.import_module and (not self.node_class or ":" not in self.node_class):
            return ""
        module = self.import_module or self.node_class.split(":", 1)[0]
        names = [
            schema.get("title", "")
            for schema in (self.input_schema, self.output_schema)
        ]
        listed = ", ".join(n for n in names if n)
        return f"from {module} import {listed}" if listed else ""
