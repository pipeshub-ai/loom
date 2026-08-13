"""Toolset catalog — three-tier lazy disclosure for the coding agent.

Tier 1 (search): ~40 tokens per hit — enough for the agent to pick a toolset.
Tier 2 (show):   ~300-900 tokens — operation table for a toolset or group.
Tier 3 (stub):   ~250-500 tokens — typed contract for a single operation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from workflow_builder.toolsets.manifest import EffectClass, ToolsetManifest

# ---------------------------------------------------------------------------
# Tier 1 — Index Card
# ---------------------------------------------------------------------------


class IndexCard(BaseModel):
    """Compact summary returned by ``catalog.search()``."""

    toolset_id: str
    summary: str
    groups: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tier 2 — Operation Table
# ---------------------------------------------------------------------------


class OpSummary(BaseModel):
    """One row in an operation table."""

    id: str
    summary: str
    effect: EffectClass = EffectClass.READ


class OpTable(BaseModel):
    """Operation table for a toolset or group."""

    toolset_id: str
    ops: list[OpSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tier 3 — Operation Contract
# ---------------------------------------------------------------------------


class OpContract(BaseModel):
    """Full typed contract for a single operation."""

    op_id: str
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)
    effect: EffectClass = EffectClass.READ
    description: str = ""
    idempotent: bool = False
    pagination: bool = False


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ToolsetCatalog:
    """Serves toolset information at three tiers of detail."""

    def __init__(self) -> None:
        self._manifests: dict[str, ToolsetManifest] = {}

    def register(self, manifest: ToolsetManifest, /) -> None:
        """Register a toolset manifest."""
        self._manifests[manifest.id] = manifest

    def unregister(self, toolset_id: str) -> None:
        """Remove a toolset from the catalog."""
        self._manifests.pop(toolset_id, None)

    def get(self, toolset_id: str) -> ToolsetManifest | None:
        """Retrieve a manifest by id."""
        return self._manifests.get(toolset_id)

    @property
    def toolset_ids(self) -> list[str]:
        """List all registered toolset ids."""
        return list(self._manifests)

    # -- Tier 1 ---------------------------------------------------------------

    def search(self, query: str, *, limit: int = 10) -> list[IndexCard]:
        """Return index cards matching *query* (~40 tokens each)."""
        query_lower = query.lower()
        terms = query_lower.split()
        results: list[tuple[int, IndexCard]] = []
        for m in self._manifests.values():
            score = self._score(terms, m)
            if score > 0:
                card = IndexCard(
                    toolset_id=m.id,
                    summary=m.summary,
                    groups=list(m.groups.keys()),
                )
                results.append((score, card))
        results.sort(key=lambda x: x[0], reverse=True)
        return [card for _, card in results[:limit]]

    # -- Tier 2 ---------------------------------------------------------------

    def show(
        self, toolset_id: str, group: str | None = None
    ) -> OpTable:
        """Return the operation table for a toolset or group."""
        manifest = self._manifests.get(toolset_id)
        if manifest is None:
            msg = f"Toolset '{toolset_id}' not found"
            raise KeyError(msg)
        ops = manifest.groups.get(group, []) if group is not None else manifest.all_operations()
        return OpTable(
            toolset_id=toolset_id,
            ops=[
                OpSummary(id=op.id, summary=op.summary, effect=op.effect)
                for op in ops
            ],
        )

    # -- Tier 3 ---------------------------------------------------------------

    def stub(self, op_path: str) -> OpContract:
        """Return the typed contract for a single operation.

        *op_path* is ``toolset_id.op_id`` — e.g. ``salesforce.leads.upsert``.
        The first dotted segment is the toolset id; the rest is the op id.
        """
        dot = op_path.find(".")
        if dot == -1:
            msg = (
                f"Invalid op path '{op_path}' — "
                "expected 'toolset_id.op_id'"
            )
            raise ValueError(msg)
        toolset_id = op_path[:dot]
        op_id = op_path[dot + 1 :]
        manifest = self._manifests.get(toolset_id)
        if manifest is None:
            msg = f"Toolset '{toolset_id}' not found"
            raise KeyError(msg)
        op = manifest.find_operation(op_id)
        if op is None:
            msg = f"Operation '{op_id}' not found in toolset '{toolset_id}'"
            raise KeyError(msg)
        return OpContract(
            op_id=op.id,
            input_schema=op.input_schema,
            output_schema=op.output_schema,
            scopes=op.scopes,
            effect=op.effect,
            description=op.description,
            idempotent=op.idempotent,
            pagination=op.pagination,
        )

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _score(terms: list[str], manifest: ToolsetManifest) -> int:
        """Simple term-frequency scoring."""
        haystack = " ".join([
            manifest.id,
            manifest.summary,
            manifest.description,
            " ".join(manifest.groups.keys()),
            " ".join(
                op.summary
                for ops in manifest.groups.values()
                for op in ops
            ),
        ]).lower()
        return sum(1 for t in terms if t in haystack)
