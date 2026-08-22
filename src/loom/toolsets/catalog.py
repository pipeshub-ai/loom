"""Toolset catalog — three-tier lazy disclosure for the coding agent.

Tier 1 (search): ~40 tokens per hit — enough for the agent to pick a toolset.
Tier 2 (show):   ~300-900 tokens — operation table for a toolset or group.
Tier 3 (stub):   ~250-500 tokens — typed contract for a single operation.
"""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, Field

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest

# ---------------------------------------------------------------------------
# Tier 1 — Index Card
# ---------------------------------------------------------------------------


class OpMatch(BaseModel):
    """One operation that matched an operation-level search."""

    toolset_id: str
    op_id: str
    summary: str = ""
    effect: str = ""
    resolves: str = ""
    """The entity kind this operation resolves, when it resolves one — the
    signal that turns "filter on a name" into "look the name up first"."""
    import_line: str = ""
    """What generated code has to write to call it."""


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
        self._by_function_manifest: dict[str, ToolsetManifest] | None = None
        """Lazy ``@step`` function name → the manifest that declares it."""
        self._by_function_op: dict[str, OperationSpec] | None = None
        """Lazy ``@step`` function name → its whole :class:`OperationSpec`,
        which is what :meth:`profile_of` needs. Kept beside ``_by_function``
        rather than replacing it because ``effect_of`` is the narrow, hot
        lookup the broker makes per dispatch, and it should not pay for
        building a profile."""

    def register(self, manifest: ToolsetManifest, /) -> None:
        """Register a toolset manifest."""
        self._manifests[manifest.id] = manifest
        self.invalidate()

    def unregister(self, toolset_id: str) -> None:
        """Remove a toolset from the catalog."""
        self._manifests.pop(toolset_id, None)
        self.invalidate()

    def invalidate(self) -> None:
        """Drop the derived function indexes. Call after touching ``_manifests``.

        One method rather than the pair of assignments repeated at each write,
        so a third index cannot be added and forgotten at one of them — and so
        anything that manipulates the store directly has a supported way to say
        so. ``tests/conftest.py`` snapshots and restores the process-global
        catalogue between tests and did exactly that: it put ``_manifests``
        back and left ``_by_function_op`` holding 358 entries, so a test that
        registered toolsets leaked their effect classification into every test
        that ran after it.
        """
        self._by_function = None
        self._by_function_op = None
        self._by_function_manifest = None

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

    def manifest_of(self, function: str) -> ToolsetManifest | None:
        """The toolset a ``@step`` function belongs to, or ``None``.

        The reverse of :meth:`effect_of`, one level up: that answers *what* an
        operation does, this answers *whose* it is — which is what a caller
        needs to say "jira is not connected" rather than "JIRA_URL is
        required". Shares the same lazy index, so it costs one dict lookup
        after the first call.

        Manifest metadata only; no toolset is imported to answer.
        """
        if self._by_function_manifest is None:
            self._by_function_manifest = {
                operation.function: manifest
                for manifest in self._manifests.values()
                for operation in manifest.all_operations()
                if operation.function
            }
        return self._by_function_manifest.get(function)

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
        results: list[tuple[float, IndexCard]] = []
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

    def search_operations(
        self, query: str, *, limit: int = 10, toolset_id: str | None = None
    ) -> list[OpMatch]:
        """Find individual *operations* matching *query*, across every toolset.

        The gap this fills: toolset-level search answers "is there a Jira
        integration", and the coding agent's next question is "which of Jira's
        forty operations transitions an issue". Only ``show_toolset`` answered
        that, and only by listing all forty — so a model either read the whole
        table or guessed. Nothing searched at this granularity.

        Scored the same way as :meth:`search`, over the operation's own id,
        summary, description and the entity kind it resolves.
        """
        terms = _terms(query)
        matches: list[tuple[float, OpMatch]] = []
        for manifest in self._manifests.values():
            if toolset_id is not None and manifest.id != toolset_id:
                continue
            for op in manifest.all_operations():
                score = _score_text(terms, _operation_text(manifest, op))
                if score <= 0:
                    continue
                matches.append((
                    score,
                    OpMatch(
                        toolset_id=manifest.id,
                        op_id=op.id,
                        summary=op.summary,
                        effect=op.effect,
                        resolves=op.resolves or "",
                        import_line=_import_for(manifest, op),
                    ),
                ))
        matches.sort(key=lambda pair: (-pair[0], pair[1].toolset_id, pair[1].op_id))
        return [match for _, match in matches[:limit]]

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _score(terms: list[str], manifest: ToolsetManifest) -> float:
        """Term-frequency scoring, normalised by how much text a manifest has.

        Normalisation matters here and did not exist: the haystack for a large
        integration is many times longer than for a small one, so an unnormalised
        count ranked whichever toolset simply had the most prose. Salesforce
        outranking DuckDuckGo for "search the web" is not a relevance judgement.
        """
        return _score_text(terms, _manifest_text(manifest))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")

#: Words that match everything and therefore rank nothing. Dropped before
#: scoring so "search the web for news" is scored on "search", "web" and
#: "news" rather than being diluted by "the" and "for".
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "that", "the", "then", "this", "to",
    "with",
})


def _terms(query: str) -> list[str]:
    """The words worth scoring, lowercased and de-duplicated in order."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(query.lower()):
        if word not in _STOPWORDS and len(word) > 1:
            seen.setdefault(word, None)
    return list(seen)


def _score_text(terms: list[str], text: str) -> float:
    """How well *text* answers *terms*, normalised for length.

    A hit is worth one point; the total is then divided by the square root of
    the text's word count, which is the standard correction for the fact that a
    longer document matches more terms by accident. Without it, ranking is a
    proxy for how much documentation a toolset happens to carry.
    """
    if not terms or not text:
        return 0.0
    words = _WORD.findall(text)
    if not words:
        return 0.0
    present = set(words)
    hits = sum(1 for term in terms if term in present)
    # Substring hits count for less: "list" inside "blocklist" is a weaker
    # signal than the word itself, but discarding it entirely loses stemming.
    partial = sum(0.5 for term in terms if term not in present and term in text)
    if hits + partial == 0:
        return 0.0
    return (hits + partial) / math.sqrt(len(words))


def _manifest_text(manifest: ToolsetManifest) -> str:
    return " ".join([
        manifest.id,
        manifest.summary,
        manifest.description,
        " ".join(manifest.groups.keys()),
        " ".join(op.summary for op in manifest.all_operations()),
    ]).lower()


def _operation_text(manifest: ToolsetManifest, op: Any) -> str:
    return " ".join([
        manifest.id,
        op.id,
        op.summary or "",
        op.description or "",
        op.resolves or "",
    ]).lower()


def _import_for(manifest: ToolsetManifest, op: Any) -> str:
    """The import line for *one* operation.

    ``ToolsetManifest.import_line()`` composes every function in the toolset,
    which is right for the docs block and wrong for a search result: a match on
    one operation should not hand the model forty names to choose from.
    """
    if not manifest.tools_module or not op.function:
        return ""
    return f"from {manifest.tools_module} import {op.function}"
