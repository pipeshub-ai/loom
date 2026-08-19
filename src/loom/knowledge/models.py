"""The shapes a knowledge base moves around.

Three, and the split between them is the point: a :class:`Chunk` is text
somebody produced, a :class:`Vector` is what an embedding model made of it, and
a :class:`Match` is one answer to a query with the *distance* attached. Fusing
the last two is the mistake that makes a search result look authoritative — a
match with no score cannot be thresholded, and an unthresholded RAG answer is
the model confidently citing the least-bad row in the index.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Chunk", "Match", "Vector", "cosine", "normalise"]

#: An embedding, as a plain list of floats.
#:
#: Not a numpy array on purpose. LOOM's core has no numpy dependency, a journal
#: has to serialise this, and the reference store's arithmetic is fast enough
#: for the sizes an embedded workflow indexes. A host with a million vectors
#: implements :class:`~loom.knowledge.store.VectorStore` over something that
#: does use numpy, which is exactly what the port is for.
Vector = list[float]


class Chunk(BaseModel):
    """One indexable piece of text, and where it came from."""

    id: str = ""
    """Stable across re-indexing the same content. Derived from the text and
    the source when not given, so re-running an ingest updates rows rather than
    duplicating them — see :meth:`with_derived_id`."""

    text: str
    source: str = ""
    """What this came from — a URL, a filename, a document id."""

    ordinal: int = 0
    """Position within the source, so a citation can say "page 3" and a caller
    can re-assemble neighbouring chunks in order."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Anything a query might filter on. Kept beside the vector rather than in
    a second store, because a filter that has to join two stores is a filter
    nobody applies."""

    def with_derived_id(self) -> Chunk:
        """A copy whose id is a digest of its content and position.

        The property that makes re-ingesting cheap: identical text from the
        same place at the same ordinal produces the same id, so an upsert
        overwrites instead of appending. A random id would make every re-run
        double the index while looking like it worked.
        """
        if self.id:
            return self
        digest = hashlib.sha256(
            f"{self.source}\x00{self.ordinal}\x00{self.text}".encode()
        ).hexdigest()[:32]
        return self.model_copy(update={"id": digest})


class Match(BaseModel):
    """One row a query found, and how close it actually was."""

    chunk: Chunk
    score: float
    """Cosine similarity, in ``[-1, 1]`` — higher is closer.

    Carried rather than dropped because a search always returns *something*.
    With ``top_k=5`` against an index of six unrelated documents, five of them
    come back, and only the score says they are all wrong. A workflow that
    feeds them to a model without thresholding gets a confident answer built
    from nothing relevant."""


def normalise(vector: Vector) -> Vector:
    """A unit-length copy, or the zero vector unchanged.

    Normalising once at write time makes every later comparison a dot product
    rather than a division, and — more importantly — makes the zero vector's
    behaviour explicit in one place. A zero vector has no direction, so its
    similarity to anything is undefined; returning it unchanged means
    :func:`cosine` can answer ``0.0`` rather than raising deep inside a query.
    """
    length = math.sqrt(sum(component * component for component in vector))
    if length == 0.0:
        return list(vector)
    return [component / length for component in vector]


def cosine(left: Vector, right: Vector) -> float:
    """Cosine similarity, safe on zero vectors and mismatched lengths.

    Returns ``0.0`` rather than raising when either side has no magnitude —
    "not similar" is the honest answer for a vector with no direction, and a
    raise here surfaces several frames from whatever produced the empty
    embedding.

    A length mismatch **does** raise: two different embedding models produce
    two different spaces, and comparing across them yields a plausible number
    that means nothing. That is a configuration error, and finding it at the
    first query beats a search that quietly ranks by noise.
    """
    if len(left) != len(right):
        raise ValueError(
            f"cannot compare a {len(left)}-dimension vector with a "
            f"{len(right)}-dimension one. Two embedding models produce two "
            "different spaces, and a similarity across them is a number that "
            "means nothing — re-index with a single model."
        )
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_len = math.sqrt(sum(a * a for a in left))
    right_len = math.sqrt(sum(b * b for b in right))
    if left_len == 0.0 or right_len == 0.0:
        return 0.0
    return dot / (left_len * right_len)
