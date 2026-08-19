"""The embedding port, and the two things a caller must not get wrong.

``EmbeddingProvider`` is one method, beside ``ModelProvider`` and for the same
reason: everything else — batching, durability, cost — belongs to the layer
above, so swapping vendors is a one-line change.

**Two texts embedded by two different models cannot be compared.** The numbers
come back the same shape and the arithmetic succeeds, so the failure is a
search that ranks by noise and reports scores that look ordinary. Every
provider here therefore carries ``model_name`` and ``dimensions``, and
:class:`~loom.knowledge.store.VectorStore` records the model an index was built
with — a mismatch is refused rather than computed.

**A query is embedded differently from a document by some providers.** Those
that distinguish take a task type; the two calls are separate methods here
(:meth:`embed_documents` and :meth:`embed_query`) so a provider that needs the
distinction can make it and one that does not can alias them. Collapsing them
into one method means the distinction can only be made by the caller, who does
not know which providers need it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from loom.core.exceptions import ConfigurationError
from loom.knowledge.models import Vector, normalise

__all__ = ["EmbeddingProvider", "MockEmbeddings", "embed_in_batches"]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors. One integration point per vendor."""

    model_name: str
    """Recorded on every index built with it. A search across two models is
    refused on this, not on the dimension count — two models can share a
    dimension count and still occupy different spaces."""

    dimensions: int

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        """Embed text that is being *stored*."""
        ...

    async def embed_query(self, text: str) -> Vector:
        """Embed text that is being *searched for*.

        Separate from :meth:`embed_documents` because several providers ask
        which it is and produce different vectors accordingly. A provider that
        does not distinguish implements this as a one-element call to the
        other.
        """
        ...


async def embed_in_batches(
    provider: EmbeddingProvider, texts: Sequence[str], *, batch_size: int = 96
) -> list[Vector]:
    """Embed *texts*, a batch at a time, preserving order.

    Batching lives here rather than in each provider because every vendor caps
    a request and the cap is the only thing that differs. Order is preserved by
    construction — a provider that returned them shuffled would be a defect,
    and a caller has no way to detect one, so the batches are concatenated
    rather than matched up by content.
    """
    if not texts:
        return []
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    out: list[Vector] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        vectors = await provider.embed_documents(batch)
        if len(vectors) != len(batch):
            raise ConfigurationError(
                f"{type(provider).__name__} returned {len(vectors)} vectors for "
                f"{len(batch)} texts. Every text must get one, in order — a "
                "short answer would silently mis-align every chunk after it."
            )
        out.extend(vectors)
    return out


class MockEmbeddings:
    """A deterministic provider for tests. **Not** a model.

    Hashes the text into a fixed-dimension vector, so the same text always
    embeds identically and two different texts almost never collide. That makes
    a search assertable without a network, a key, or a tolerance.

    What it deliberately does *not* do is capture meaning: "cat" and "kitten"
    are as far apart here as "cat" and "tax law". A test asserting that
    semantically similar text ranks together is asserting something this cannot
    provide, and would pass or fail on hash luck — use exact text for the row
    you expect to find.
    """

    def __init__(self, *, dimensions: int = 64, model_name: str = "mock") -> None:
        self.dimensions = dimensions
        self.model_name = model_name

    def _vector(self, text: str) -> Vector:
        # Four digest bytes per component, read as an **unsigned integer** and
        # mapped into [-1, 1].
        #
        # Not `struct.unpack("f", ...)` over the raw bytes, which was the first
        # version and was wrong: an arbitrary 32-bit pattern is a NaN or an
        # infinity often enough to matter, and one NaN component makes the
        # whole normalised vector NaN. Every score against it then comes back
        # NaN, every comparison with NaN is False, and the ranking silently
        # collapses — from a *test* provider, which is the worst place for it.
        needed = self.dimensions * 4
        material = b""
        counter = 0
        while len(material) < needed:
            material += hashlib.sha256(f"{counter}\x00{text}".encode()).digest()
            counter += 1
        components = [
            (int.from_bytes(material[i : i + 4], "big") / 0xFFFFFFFF) * 2.0 - 1.0
            for i in range(0, needed, 4)
        ]
        # Normalised, so scores land in a readable range.
        return normalise(components)

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> Vector:
        return self._vector(text)


def require_embeddings(provider: Any | None) -> EmbeddingProvider:
    """The provider, or a message naming what to pass.

    A search with no embedding provider is the failure that otherwise surfaces
    as ``AttributeError: 'NoneType' object has no attribute 'embed_query'``
    from inside a node, which reads as a broken node rather than an
    unconfigured Runtime.
    """
    if provider is None:
        raise ConfigurationError(
            "this Runtime has no embedding provider, so nothing can be indexed "
            "or searched. Pass Runtime(embeddings=...) — OpenAIEmbeddings() or "
            "GeminiEmbeddings() for real work, MockEmbeddings() in tests."
        )
    return provider  # type: ignore[no-any-return]
