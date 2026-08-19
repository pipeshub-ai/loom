"""Toolset catalog — three-tier lazy disclosure for the coding agent.

Tier 1 (search): ~40 tokens per hit — enough for the agent to pick a toolset.
Tier 2 (show):   ~300-900 tokens — operation table for a toolset or group.
Tier 3 (stub):   ~250-500 tokens — typed contract for a single operation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest

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
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
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
        self._by_function: dict[str, EffectClass] | None = None
        """Lazy ``@step`` function name → declared effect class. ``None`` means
        not built yet; invalidated on every registration."""
        self._by_function_op: dict[str, OperationSpec] | None = None
        """Lazy ``@step`` function name → its whole :class:`OperationSpec`,
        which is what :meth:`profile_of` needs. Kept beside ``_by_function``
        rather than replacing it because ``effect_of`` is the narrow, hot
        lookup the broker makes per dispatch, and it should not pay for
        building a profile."""

    def register(self, manifest: ToolsetManifest, /) -> None:
        """Register a toolset manifest."""
        self._manifests[manifest.id] = manifest
        self._by_function = None
        self._by_function_op = None

    def unregister(self, toolset_id: str) -> None:
        """Remove a toolset from the catalog."""
        self._manifests.pop(toolset_id, None)
        self._by_function = None
        self._by_function_op = None

    def effect_of(self, function: str) -> EffectClass | None:
        """Whether a ``@step`` function reads, writes, or destroys.

        The reverse of ``import_line()``: given the name a workflow body calls,
        report what its manifest declared it to be. The effect class lives on
        the manifest's :class:`OperationSpec` and nothing carried it to the call
        site, so ``await ctx.step(gmail_search_messages, ...)`` reached the
        broker classified as a *write* — the default for an operation nobody
        classified. Anything deciding on reads versus writes was therefore
        deciding on that default, and a read could never be recognised as one.

        Manifest metadata only, and no toolset is imported to answer — the same
        rule grant validation follows, and what keeps Layer 1 Layer 1.
        """
        if self._by_function is None:
            self._by_function = {
                operation.function: operation.effect
                for manifest in self._manifests.values()
                for operation in manifest.all_operations()
                if operation.function
            }
        return self._by_function.get(function)

    def profile_of(self, function: str) -> Any:
        """Every facet of a ``@step`` function's side effect, as one value.

        Added once four facets existed: a separate ``*_of`` per facet meant the
        call site grew a line for each, and a fifth would have meant editing
        every consumer again. Returns an
        :class:`~loom.toolsets.effects.EffectProfile`, or ``None`` for a
        function no manifest declares.

        Manifest metadata only — no toolset is imported to answer, which is
        what keeps Layer 1 Layer 1.
        """
        from loom.toolsets.effects import derive_effect_profile

        if self._by_function_op is None:
            self._by_function_op = {
                operation.function: operation
                for manifest in self._manifests.values()
                for operation in manifest.all_operations()
                if operation.function
            }
        operation = self._by_function_op.get(function)
        if operation is None:
            return None
        return derive_effect_profile(operation)

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
