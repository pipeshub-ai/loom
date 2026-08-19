"""``knowledge.*`` — chunk, index, and search.

Three nodes, and the split between them is the "code or judgement" rule made
structural. **Chunking is a rule**, so it is deterministic and free to
recompute; **indexing and searching reach a service**, so they are not.

The pairing that matters is `index` and `search`: both take the same
``namespace``, and both record or check the embedding model. Two models occupy
two different spaces, so a search embedded by one against an index built by
another produces a plausible ranking that means nothing — refused here rather
than computed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from loom.knowledge.chunking import split_text
from loom.knowledge.embeddings import embed_in_batches, require_embeddings
from loom.knowledge.models import Chunk, Match
from loom.knowledge.store import require_vectors
from loom.nodes.base import Node, NodeContext
from loom.nodes.registry import register_node
from loom.nodes.spec import (
    EffectClass,
    NodeCategory,
    NodeExample,
    NodeSpec,
)

__all__ = ["ChunkNode", "IndexNode", "SearchNode"]


def _spec(**declared: Any) -> NodeSpec:
    return NodeSpec(
        import_module="loom.nodes.knowledge",
        category=NodeCategory.TRANSFORM,
        **declared,
    )


# ---------------------------------------------------------------------------
# knowledge.chunk
# ---------------------------------------------------------------------------


class ChunkIn(BaseModel):
    text: str = Field(description="The document text to split.")
    size: int = Field(
        default=1000,
        description=(
            "Hard ceiling on a chunk, in characters. Characters rather than "
            "tokens, because tokenising needs the model's own tokenizer."
        ),
    )
    overlap: int = Field(
        default=150,
        description=(
            "How much each chunk repeats of the last. Without it a fact that "
            "straddles a boundary is in neither chunk, so a query for it "
            "matches nothing and the run reports 'not found' about a document "
            "that plainly says it."
        ),
    )
    source: str = Field(
        default="", description="Recorded on every chunk, and part of its id."
    )


class ChunkOut(BaseModel):
    chunks: list[Chunk] = Field(default_factory=list)
    count: int = 0


@register_node
class ChunkNode(Node[ChunkIn, ChunkOut]):
    """Split a document into overlapping chunks at natural boundaries."""

    spec = _spec(
        id="knowledge.chunk",
        deterministic=True,
        summary="Split a document into overlapping chunks for indexing.",
        description=(
            "A rule, not judgement — the same text always splits the same way, "
            "so this is free to recompute on replay and needs no agent. "
            "Boundaries are preferred (paragraph, then sentence, then space) "
            "and the size cap is enforced: a chunk past the embedding model's "
            "window is silently truncated by the model, which is worse than an "
            "ugly split. Chunks carry derived ids, so re-splitting identical "
            "text produces identical ids and a re-ingest updates rather than "
            "duplicates."
        ),
        effect=EffectClass.READ,
        tags=["chunk", "split", "rag", "index", "document"],
        examples=[
            NodeExample(payload={"text": "…", "size": 800, "overlap": 100})
        ],
    )
    Input, Output = ChunkIn, ChunkOut

    async def run(self, ctx: NodeContext, payload: ChunkIn) -> ChunkOut:
        chunks = split_text(
            payload.text,
            size=payload.size,
            overlap=payload.overlap,
            source=payload.source,
        )
        return ChunkOut(chunks=chunks, count=len(chunks))


# ---------------------------------------------------------------------------
# knowledge.index
# ---------------------------------------------------------------------------


class IndexIn(BaseModel):
    namespace: str = Field(
        description=(
            "Which index to write to. One namespace per corpus — a session, a "
            "document set, a tenant. It also records the embedding model, and "
            "a write from a second model is refused."
        )
    )
    chunks: list[Chunk] = Field(default_factory=list)
    batch_size: int = Field(
        default=96, description="Texts per embedding request."
    )


class IndexOut(BaseModel):
    namespace: str = ""
    indexed: int = 0
    model: str = ""
    """The embedding model used, recorded so a later search can be checked
    against it."""


@register_node
class IndexNode(Node[IndexIn, IndexOut]):
    """Embed chunks and write them to a vector store."""

    spec = _spec(
        id="knowledge.index",
        deterministic=False,
        summary="Embed chunks and write them to a vector namespace.",
        description=(
            "Needs Runtime(embeddings=..., vectors=...). Keyed by chunk id, so "
            "re-indexing identical content overwrites rather than duplicating "
            "— which is what makes an ingest safe to re-run. Records the "
            "embedding model on the namespace; a write from a different model "
            "is refused, because two models occupy two different spaces and a "
            "mixed index ranks by noise while reporting ordinary scores."
        ),
        effect=EffectClass.WRITE,
        requires=["embeddings", "vectors"],
        tags=["index", "embed", "rag", "vector", "upsert"],
        examples=[
            NodeExample(payload={"namespace": "session-1", "chunks": []})
        ],
    )
    Input, Output = IndexIn, IndexOut

    async def run(self, ctx: NodeContext, payload: IndexIn) -> IndexOut:
        provider = require_embeddings(ctx.capability("embeddings"))
        vectors = require_vectors(ctx.capability("vectors"))
        if not payload.chunks:
            return IndexOut(namespace=payload.namespace, model=provider.model_name)

        embedded = await embed_in_batches(
            provider,
            [chunk.text for chunk in payload.chunks],
            batch_size=payload.batch_size,
        )
        written = await vectors.upsert(
            payload.namespace,
            payload.chunks,
            embedded,
            model=provider.model_name,
        )
        return IndexOut(
            namespace=payload.namespace,
            indexed=written,
            model=provider.model_name,
        )


# ---------------------------------------------------------------------------
# knowledge.search
# ---------------------------------------------------------------------------


class SearchIn(BaseModel):
    namespace: str = Field(description="Which index to search.")
    query: str = Field(description="What to look for, in plain language.")
    top_k: int = Field(default=5, description="How many matches to return.")
    min_score: float = Field(
        default=0.0,
        description=(
            "Drop matches below this cosine similarity. A search always "
            "returns something: with top_k=5 against six unrelated documents, "
            "five come back and only the score says they are all wrong. "
            "Feeding those to a model gets a confident answer built from "
            "nothing relevant."
        ),
    )
    where: dict[str, Any] = Field(
        default_factory=dict,
        description="Exact-match filter over chunk metadata.",
    )


class SearchOut(BaseModel):
    matches: list[Match] = Field(default_factory=list)
    found: int = 0
    dropped_below_threshold: int = 0
    """How many matches `min_score` removed.

    Reported rather than discarded silently: zero results *because everything
    scored badly* is a different fact from an empty index, and a workflow
    should be able to say which."""


@register_node
class SearchNode(Node[SearchIn, SearchOut]):
    """Find the chunks closest to a query."""

    spec = _spec(
        id="knowledge.search",
        deterministic=False,
        summary="Find the chunks closest to a query, with their scores.",
        description=(
            "Needs Runtime(embeddings=..., vectors=...). Returns each match "
            "with its cosine similarity, and min_score is how a workflow "
            "refuses a bad one — a search always returns something, and "
            "unthresholded RAG is the model citing the least-bad row in the "
            "index. The count dropped by the threshold is reported, because "
            "'nothing scored well' and 'the index is empty' are different "
            "facts."
        ),
        effect=EffectClass.READ,
        requires=["embeddings", "vectors"],
        tags=["search", "retrieve", "rag", "vector", "semantic"],
        examples=[
            NodeExample(
                payload={
                    "namespace": "session-1",
                    "query": "what is the notice period?",
                    "top_k": 4,
                    "min_score": 0.2,
                }
            )
        ],
    )
    Input, Output = SearchIn, SearchOut

    async def run(self, ctx: NodeContext, payload: SearchIn) -> SearchOut:
        provider = require_embeddings(ctx.capability("embeddings"))
        vectors = require_vectors(ctx.capability("vectors"))

        embedded = await provider.embed_query(payload.query)
        matches = await vectors.query(
            payload.namespace,
            embedded,
            top_k=payload.top_k,
            where=payload.where or None,
            model=provider.model_name,
        )
        kept = [m for m in matches if m.score >= payload.min_score]
        return SearchOut(
            matches=kept,
            found=len(kept),
            dropped_below_threshold=len(matches) - len(kept),
        )
