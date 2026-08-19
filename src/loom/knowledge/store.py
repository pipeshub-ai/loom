"""The vector-store port, and one reference implementation.

**The package ships no vector database.** It ships this protocol, a store built
only on capabilities every LOOM store already has, and a conformance kit
(``loom.testing.conformance.verify_vector_store``) so a host proves its own
pgvector, Pinecone, or Qdrant adapter correct. That is the same position
``loom/events/`` takes about brokers, for the same reason: every adapter
shipped is one that must be tested against a real server forever.

:class:`StoreBackedVectorStore` is the reference. It scans — every query
compares against every vector in the namespace — which is honest for the sizes
an embedded workflow indexes and wrong for a million rows. A host with a
million rows implements the port; that is what it is for. The scan is not a
placeholder for an index nobody wrote, it is the correct implementation for a
store whose only primitives are get and set.

**A namespace records the model that built it.** Two embedding models produce
two different spaces, and comparing across them yields a plausible number that
means nothing — so writing a vector from a second model into an existing
namespace is refused, rather than ranked.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from loom.core.exceptions import ConfigurationError
from loom.knowledge.models import Chunk, Match, Vector, cosine

__all__ = ["StoreBackedVectorStore", "VectorStore"]

logger = logging.getLogger("workflow.knowledge")

#: How a namespace's index and its metadata are keyed in the backing store.
_INDEX = "loom:vectors:{namespace}:index"
_META = "loom:vectors:{namespace}:meta"


@runtime_checkable
class VectorStore(Protocol):
    """Somewhere vectors live, and can be searched by similarity."""

    async def upsert(
        self, namespace: str, chunks: Sequence[Chunk], vectors: Sequence[Vector], *, model: str
    ) -> int:
        """Write chunks and their vectors. Returns how many rows were stored.

        Keyed by ``Chunk.id``, so re-indexing identical content overwrites
        rather than duplicating — which is what makes an ingest re-runnable.

        *model* names the embedding model. An implementation must refuse a
        write whose model differs from the namespace's, because a namespace
        holding two models' vectors ranks by noise and reports ordinary-looking
        scores.
        """
        ...

    async def query(
        self,
        namespace: str,
        vector: Vector,
        *,
        top_k: int = 5,
        where: Mapping[str, Any] | None = None,
        model: str = "",
    ) -> list[Match]:
        """The *top_k* closest rows, each with its score.

        *where* filters on ``Chunk.metadata`` by exact match. Filtering is part
        of the port rather than left to the caller because a store that can
        push it down should, and one that cannot can apply it after the scan —
        either way the caller gets ``top_k`` matching rows rather than ``top_k``
        rows of which some match.
        """
        ...

    async def delete(self, namespace: str, ids: Sequence[str] | None = None) -> int:
        """Delete rows by id, or the whole namespace when *ids* is ``None``."""
        ...

    async def count(self, namespace: str) -> int:
        """How many rows the namespace holds."""
        ...


class StoreBackedVectorStore:
    """A vector store over :class:`~loom.stores.base.CacheStore`.

    The same move ``ctx.state`` and the event log made: whatever backs the
    journal backs this, so a laptop gets a durable index out of one SQLite file
    and a deployment gets one out of the database it already runs — with no
    fifth service to operate.

    Two limits, stated rather than discovered:

    * **It scans.** Every query compares against every vector in the namespace.
      Fine at thousands, wrong at millions.
    * **A namespace is read and written whole.** Two processes upserting the
      same namespace concurrently can lose one of the writes. A host that needs
      concurrent ingest wants a real vector database, which is what the port is
      for.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    async def _read(self, namespace: str) -> dict[str, dict[str, Any]]:
        raw = await self._store.get(_INDEX.format(namespace=namespace))
        return dict(raw) if isinstance(raw, dict) else {}

    async def _write(self, namespace: str, rows: dict[str, dict[str, Any]]) -> None:
        await self._store.set(_INDEX.format(namespace=namespace), rows, 0)

    async def model_of(self, namespace: str) -> str:
        """Which embedding model built this namespace, or ``""``."""
        meta = await self._store.get(_META.format(namespace=namespace))
        return str((meta or {}).get("model") or "") if isinstance(meta, dict) else ""

    async def upsert(
        self,
        namespace: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Vector],
        *,
        model: str,
    ) -> int:
        if len(chunks) != len(vectors):
            raise ValueError(
                f"{len(chunks)} chunks and {len(vectors)} vectors — every chunk "
                "needs exactly one, and a mismatch would pair each chunk with "
                "another one's meaning."
            )
        if not chunks:
            return 0

        existing = await self.model_of(namespace)
        if existing and model and existing != model:
            raise ConfigurationError(
                f"namespace {namespace!r} was indexed with {existing!r} and this "
                f"write uses {model!r}. Two embedding models occupy two "
                "different spaces, so a search across them ranks by noise and "
                "reports ordinary-looking scores. Delete the namespace and "
                "re-index, or write to a different one."
            )

        rows = await self._read(namespace)
        for chunk, vector in zip(chunks, vectors, strict=True):
            identified = chunk.with_derived_id()
            rows[identified.id] = {
                "chunk": identified.model_dump(mode="json"),
                "vector": list(vector),
            }
        await self._write(namespace, rows)
        if model:
            await self._store.set(
                _META.format(namespace=namespace), {"model": model}, 0
            )
        return len(chunks)

    async def query(
        self,
        namespace: str,
        vector: Vector,
        *,
        top_k: int = 5,
        where: Mapping[str, Any] | None = None,
        model: str = "",
    ) -> list[Match]:
        existing = await self.model_of(namespace)
        if existing and model and existing != model:
            raise ConfigurationError(
                f"namespace {namespace!r} was indexed with {existing!r} and this "
                f"query embeds with {model!r}. The arithmetic would succeed and "
                "the ranking would be meaningless."
            )

        rows = await self._read(namespace)
        matches: list[Match] = []
        for row in rows.values():
            chunk = Chunk.model_validate(row["chunk"])
            if where and any(chunk.metadata.get(k) != v for k, v in where.items()):
                continue
            matches.append(Match(chunk=chunk, score=cosine(vector, row["vector"])))

        # Ties broken by id, so a repeated query returns a repeated order. An
        # unstable order across identical queries makes a replayed run cite a
        # different chunk, which is a divergence nothing else would flag.
        matches.sort(key=lambda m: (-m.score, m.chunk.id))
        return matches[: max(top_k, 0)]

    async def delete(self, namespace: str, ids: Sequence[str] | None = None) -> int:
        if ids is None:
            rows = await self._read(namespace)
            await self._store.delete(_INDEX.format(namespace=namespace))
            await self._store.delete(_META.format(namespace=namespace))
            return len(rows)

        rows = await self._read(namespace)
        removed = 0
        for identifier in ids:
            if rows.pop(identifier, None) is not None:
                removed += 1
        await self._write(namespace, rows)
        return removed

    async def count(self, namespace: str) -> int:
        return len(await self._read(namespace))


def require_vectors(store: Any | None) -> VectorStore:
    """The store, or a message naming what to pass."""
    if store is None:
        raise ConfigurationError(
            "this Runtime has no vector store, so nothing can be indexed or "
            "searched. Pass Runtime(vectors=StoreBackedVectorStore(store)) — "
            "it needs no service beyond the one already backing the journal."
        )
    return store  # type: ignore[no-any-return]
