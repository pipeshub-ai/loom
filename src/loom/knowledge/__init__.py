"""Retrieval primitives — chunk, embed, index, search.

**LOOM ships no vector database.** It ships two ports, a reference store built
only on capabilities every LOOM store already has, and a conformance kit
(``loom.testing.conformance.verify_vector_store``) so a host proves its own
pgvector, Pinecone, or Qdrant adapter correct. That is the position
``loom/events/`` takes about brokers, for the same reason.

    rt = Runtime(
        store=store,
        embeddings=MockEmbeddings(),
        vectors=StoreBackedVectorStore(store),
    )

Both default to ``None``, and nothing is enforced or configured unless a host
composes them in. The three ``knowledge.*`` nodes are how a workflow reaches
them.
"""

from __future__ import annotations

from loom.knowledge.chunking import split_text
from loom.knowledge.embeddings import (
    EmbeddingProvider,
    MockEmbeddings,
    embed_in_batches,
)
from loom.knowledge.models import Chunk, Match, Vector, cosine, normalise
from loom.knowledge.store import StoreBackedVectorStore, VectorStore

__all__ = [
    "Chunk",
    "EmbeddingProvider",
    "Match",
    "MockEmbeddings",
    "StoreBackedVectorStore",
    "Vector",
    "VectorStore",
    "cosine",
    "embed_in_batches",
    "normalise",
    "split_text",
]
