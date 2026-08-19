"""Splitting text for indexing.

A rule, not judgement — so this is a plain function and a ``transform`` node,
never an agent. The classification matters: an agent asked to split a document
puts a nondeterministic call into every ingest to re-answer a question that has
one right answer for a given input.

**Overlap is the whole design.** A fact that straddles a boundary is in neither
chunk if the split is clean, so a query for it matches nothing and the run
reports "not found" about a document that plainly says it. Overlapping windows
mean the fact appears whole in at least one chunk.

**Boundaries are preferred, not enforced.** The splitter looks backwards from
the hard limit for a paragraph break, then a sentence end, then a space — and
falls back to cutting mid-word rather than exceeding the limit, because a chunk
larger than the embedding model's window is silently truncated by the model,
which is worse than an ugly split.
"""

from __future__ import annotations

from loom.knowledge.models import Chunk

__all__ = ["split_text"]

#: Where a chunk would rather end, best first. Each is searched for in the tail
#: of the window, so a paragraph break beats a sentence end beats a space.
_BOUNDARIES = ("\n\n", "\n", ". ", "? ", "! ", "; ", " ")

#: How far back from the hard limit a boundary is worth looking for, as a
#: fraction of the chunk. Beyond this the chunks get so uneven that the overlap
#: stops covering the gaps, so the splitter takes the clean cut instead.
_LOOKBACK = 0.35


def split_text(
    text: str,
    *,
    size: int = 1000,
    overlap: int = 150,
    source: str = "",
) -> list[Chunk]:
    """Split *text* into overlapping chunks, preferring natural boundaries.

    Args:
        text: What to split.
        size: Hard ceiling on a chunk, in characters. Characters rather than
            tokens because tokenising needs the model's own tokenizer, and a
            character budget with headroom is both portable and close enough.
        overlap: How much of the previous chunk each one repeats, so a fact
            spanning a boundary survives whole somewhere.
        source: Recorded on every chunk, and part of its derived id.

    Returns chunks already carrying derived ids, so re-splitting identical text
    from the same source produces identical ids — which is what makes a
    re-ingest an update rather than a duplication.
    """
    if size < 1:
        raise ValueError("size must be at least 1")
    if overlap < 0:
        raise ValueError("overlap cannot be negative")
    if overlap >= size:
        # Otherwise each window starts at or before the previous one and the
        # loop never advances — a hang rather than a bad answer.
        raise ValueError(
            f"overlap ({overlap}) must be smaller than size ({size}), or each "
            "chunk would start where the last one did and the split would not "
            "terminate."
        )

    stripped = text.strip()
    if not stripped:
        return []

    chunks: list[Chunk] = []
    start = 0
    ordinal = 0
    while start < len(stripped):
        end = min(start + size, len(stripped))
        if end < len(stripped):
            end = _boundary(stripped, start, end, size)
        piece = stripped[start:end].strip()
        if piece:
            chunks.append(
                Chunk(text=piece, source=source, ordinal=ordinal).with_derived_id()
            )
            ordinal += 1
        if end >= len(stripped):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _boundary(text: str, start: int, end: int, size: int) -> int:
    """The best place to cut at or before *end*, or *end* itself."""
    floor = max(start + 1, end - int(size * _LOOKBACK))
    for marker in _BOUNDARIES:
        found = text.rfind(marker, floor, end)
        if found != -1:
            return found + len(marker)
    return end
