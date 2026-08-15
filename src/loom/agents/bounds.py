"""Keep an oversized tool result out of the model's context without losing it.

A tool that returns four megabytes of JSON puts four megabytes into the next
model request, and into every request after it for the rest of the agent's
turn loop. Nothing today stops that: :meth:`Tool.render_result` serializes
whatever came back and the runner appends it. The provider eventually truncates
or refuses, and the model reports on a prefix as though it were the whole —
which is the failure worth naming, because it produces a confident answer about
data the model never saw.

The fix is not truncation. Truncation alone leaves the model unable to tell
that anything is missing, and unable to do anything about it if it guesses.
What goes in front of the model instead is: a bounded head and tail, the
format, the *shape*, and a locator with the two calls that read it. The full
value is stored, and the step's journal entry keeps it whole — bounding is a
property of the conversation, not of the run.

Composable rather than configurable: a deployment that wants no storage passes
:class:`NullSpillStore` and gets truncation with an honest notice; one that
wants object storage passes :class:`BlobSpillStore`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from loom.agents.shape import describe_format, describe_shape

logger = logging.getLogger("workflow.agent.bounds")

#: What joins the kept text to the notice, and the head to the tail. Reserved
#: out of the budget so the total cannot exceed the cap — the ellipsis is three
#: bytes in UTF-8, which is the kind of detail an off-by-five is made of.
_SEPARATOR_BYTES = len("\n…\n".encode()) + len(b"\n\n")

__all__ = [
    "BlobSpillStore",
    "NullSpillStore",
    "ResultBounds",
    "SpillRef",
    "SpillStore",
]


@dataclass(frozen=True)
class SpillRef:
    """A stored oversized result, and how to read it back."""

    locator: str
    """Opaque handle. Consumers render it; they do not parse it."""
    bytes: int
    retrieval_hint: str
    """Backend-authored sentence naming the calls that read this locator."""


@runtime_checkable
class SpillStore(Protocol):
    """Persists an oversized tool result and reads it back in pieces.

    ``save`` must reject on a real storage failure rather than returning a
    locator that resolves to nothing — the policy treats a rejection as
    best-effort and keeps the inline text, which is a better outcome than a
    reference the model cannot follow.
    """

    async def save(self, text: str, *, run_id: str, tool: str, call_id: str) -> SpillRef: ...

    async def read(self, locator: str, *, offset: int = 0, limit: int = 4_000) -> str: ...

    async def grep(
        self, locator: str, pattern: str, *, max_matches: int = 50
    ) -> list[str]: ...


class NullSpillStore:
    """Bounds without storing. The default when no blob service is configured.

    Degrading to truncation-with-a-notice is the right failure: an agent told
    that 1.8 MB was omitted can narrow its query, where an agent handed a silent
    prefix cannot.
    """

    async def save(self, text: str, *, run_id: str, tool: str, call_id: str) -> SpillRef:
        raise SpillUnavailable("no spill store is configured")

    async def read(self, locator: str, *, offset: int = 0, limit: int = 4_000) -> str:
        raise SpillUnavailable("no spill store is configured")

    async def grep(
        self, locator: str, pattern: str, *, max_matches: int = 50
    ) -> list[str]:
        raise SpillUnavailable("no spill store is configured")


class SpillUnavailable(RuntimeError):  # noqa: N818 - a condition, not a fault
    """No backend could store or retrieve a spilled result."""


class BlobSpillStore:
    """Stores spilled results in LOOM's content-addressed blob service.

    Content addressing is the reason to reuse it rather than add a second
    store: a tool called twice with the same arguments produces the same bytes,
    which resolve to one blob — so retries and replays do not multiply storage.
    """

    def __init__(self, blobs: Any) -> None:
        self._blobs = blobs

    async def save(self, text: str, *, run_id: str, tool: str, call_id: str) -> SpillRef:
        raw = text.encode("utf-8")
        locator = await self._blobs.store(raw, "text/plain")
        return SpillRef(
            locator=locator,
            bytes=len(raw),
            # The locator is 69 characters and the notice is charged against
            # the same budget as the content, so it is named once and referred
            # to after that. Repeating it three times cost ~140 bytes of the
            # page the model was supposed to be reading.
            retrieval_hint=(
                "Pass that ref to read_spill(ref, offset, limit) to page it, "
                "or grep_spill(ref, pattern) to search it."
            ),
        )

    async def read(self, locator: str, *, offset: int = 0, limit: int = 4_000) -> str:
        text = (await self._blobs.load(locator)).decode("utf-8", errors="replace")
        return text[offset : offset + limit]

    async def grep(
        self, locator: str, pattern: str, *, max_matches: int = 50
    ) -> list[str]:
        text = (await self._blobs.load(locator)).decode("utf-8", errors="replace")
        try:
            # Model-supplied patterns are hostile input by default. A literal
            # fallback keeps a malformed regex from failing the call, and the
            # per-line scan bounds what a pathological pattern can cost.
            matcher = re.compile(pattern)
        except re.error:
            matcher = re.compile(re.escape(pattern))
        found: list[str] = []
        for line in text.split("\n"):
            if matcher.search(line):
                found.append(line[:2_000])
                if len(found) >= max_matches:
                    break
        return found


@dataclass(frozen=True)
class ResultBounds:
    """When a tool result is too large, and what the model sees instead.

    ``max_bytes`` is a ceiling on the *replacement*, not on the original: the
    notice's own cost is reserved out of the budget, so bounding can never
    make a result larger than the cap. That property is worth stating because
    the obvious implementation — truncate to the cap, then append a notice —
    quietly violates it.
    """

    max_bytes: int = 32_768
    max_lines: int = 2_000
    head_ratio: float = 0.67

    def within(self, text: str) -> bool:
        return (
            len(text.encode("utf-8")) <= self.max_bytes
            and text.count("\n") + 1 <= self.max_lines
        )

    def apply(self, text: str, ref: SpillRef | None, *, coverage: str = "") -> str:
        """Return the bounded replacement for *text*.

        Args:
            text: The rendered tool result.
            ref: Where the full text was stored, or ``None`` when it was not.
            coverage: A paged read's own account of what it covered. Hoisted
                into the notice ahead of any truncation, because it lives at
                the *end* of a serialized ``Results`` — exactly where a head
                and tail cut would lose it, turning one page into what reads
                as a whole set.
        """
        raw = text.encode("utf-8")
        described = describe_format(text)
        shape = describe_shape(_loads(text))
        # Size the budget against the *widest* notice this can produce: the
        # omitted count is rendered into it, and it grows a character per
        # order of magnitude. Reserving for `omitted=0` under-reserves by
        # exactly those digits and puts the replacement over the cap — which
        # is the one thing this method promises cannot happen.
        notice = self._notice(len(raw), ref, coverage, described, shape, omitted=len(raw))
        budget = self.max_bytes - len(notice.encode("utf-8")) - _SEPARATOR_BYTES
        if budget <= 0:
            # A cap so small that the notice alone exceeds it. Returning the
            # original is the lesser evil: a replacement over the cap would
            # break the one property this class promises.
            return text

        head_bytes = int(budget * self.head_ratio)
        tail_bytes = budget - head_bytes
        head = _decode_prefix(raw[:head_bytes])
        tail = _decode_suffix(raw[-tail_bytes:]) if tail_bytes > 0 else ""
        kept = f"{head}\n…\n{tail}" if tail else head
        omitted = len(raw) - len(kept.encode("utf-8"))
        tail_notice = self._notice(
            len(raw), ref, coverage, described, shape, omitted=omitted
        )
        return f"{kept}\n\n{tail_notice}"

    def _notice(
        self,
        total: int,
        ref: SpillRef | None,
        coverage: str,
        described: str,
        shape: str,
        *,
        omitted: int,
    ) -> str:
        parts: list[str] = []
        if coverage:
            parts.append(coverage)
        parts.append(f"Omitted {omitted:,} of {total:,} bytes.")
        parts.append(f"Format: {described}. Shape: {shape}.")
        if ref is not None:
            parts.append(f"Full result at {ref.locator}. {ref.retrieval_hint}")
        else:
            parts.append("The full result was not stored and cannot be retrieved.")
        parts.append("Do not re-call the tool for the full payload.")
        return f"({' '.join(parts)})"


def _decode_prefix(raw: bytes) -> str:
    """Decode bytes that may end mid-character."""
    return raw.decode("utf-8", errors="ignore")


def _decode_suffix(raw: bytes) -> str:
    """Decode bytes that may begin mid-character."""
    for start in range(min(4, len(raw))):
        try:
            return raw[start:].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def coverage_of(value: Any) -> str:
    """What a paged read says about its own completeness, or ``""``.

    Reads :class:`~loom.toolsets.pagination.Results` without
    importing it: the attributes are the contract, and a duck-typed check keeps
    this module free of a dependency on the toolset layer.
    """
    complete = getattr(value, "complete", None)
    if complete is None or not isinstance(value, list):
        return ""
    if complete:
        return f"Complete: all {len(value):,} rows."
    total = getattr(value, "total", None)
    if isinstance(total, int):
        return f"Showing {len(value):,} of {total:,} rows — this read was NOT complete."
    return f"Showing {len(value):,} rows — this read was NOT complete."


async def bound_result(
    text: str,
    value: Any,
    *,
    bounds: ResultBounds | None,
    store: SpillStore | None,
    run_id: str,
    tool: str,
    call_id: str,
) -> str:
    """Bound one rendered tool result, storing the original when possible.

    Best-effort throughout: no bounds, a small result, or a failed save all
    return something usable. A spill failure never turns a successful tool call
    into an error.
    """
    if bounds is None or bounds.within(text):
        return text

    coverage = coverage_of(value)
    ref: SpillRef | None = None
    if store is not None:
        try:
            ref = await store.save(text, run_id=run_id, tool=tool, call_id=call_id)
        except Exception as exc:
            logger.warning("could not spill %s result: %s", tool, exc)

    bounded = bounds.apply(text, ref, coverage=coverage)
    logger.info(
        "bounded %s result: %d bytes -> %d (%s)",
        tool,
        len(text.encode("utf-8")),
        len(bounded.encode("utf-8")),
        ref.locator if ref else "not stored",
    )
    return bounded


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text
