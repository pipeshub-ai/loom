"""Looking at the system a workflow is being written against.

The coding agent could always ask what *loom* offers — which toolsets exist,
what a node's contract is, whether the code compiles. It could never ask
anything about the world the code would run in. So it wrote against a
description of the target and found out whether it was right only if the code
crashed; a workflow that ran, completed, and answered wrongly looked exactly
like one that worked.

A probe closes that: read-only, structured, and carried back as a fact the model
can act on rather than a description someone typed into a spec.

**Authoring-time only.** Nothing here is reachable from a workflow body, and
nothing here is journaled. A workflow reaches the world through ``@step``,
``ctx.node`` and toolsets, which is what makes its graph a graph. A probe is the
agent *looking* before it writes, and looking is not part of what the workflow
later does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from loom.blobs.attachment import Attachment

__all__ = ["Observation", "Probe", "ProbeError"]


class ProbeError(Exception):
    """A probe could not look. Never a defect in the code being written.

    Carried as its own type so the caller can report "not observed" rather than
    folding it into the findings — the same separation ``SmokeStage`` makes for
    an environmental failure, and for the same reason: a model told that
    something is wrong will try to fix it, and there is nothing here to fix.
    """


@dataclass(frozen=True)
class Observation:
    """What one look at a target found."""

    target: str
    summary: str
    """One line, written for a model that will act on it."""
    detail: str = ""
    """The structured body. The caller decides how much of it to spend."""
    evidence: tuple[Attachment, ...] = field(default_factory=tuple)
    """Screenshots, response bodies — things worth keeping whole.

    Carried rather than described because whichever component looks first would
    otherwise flatten it to prose, and a rendered page is exactly the case where
    the prose is the part that was wrong.
    """
    probe: str = ""
    """Which probe produced this, so a surprising answer can be attributed."""


@runtime_checkable
class Probe(Protocol):
    """Look at something. Change nothing.

    Two methods rather than one ``explore()``. A probe that cannot handle a
    target has to be able to say so without being asked to guess at it, and the
    caller has to be able to pick between probes without running them — the same
    reason ``EventSource`` is four small methods instead of one ``handle()``.

    **Read-only is a property of the implementation, not a promise in the
    docstring.** A probe is handed to a model, so "please do not write" is not a
    control. ``HttpProbe`` sends GET and HEAD and has no code path that sends
    anything else; ``BrowserProbe`` navigates, reads and screenshots and never
    clicks or types. Build the capability so the unwanted call cannot be
    expressed, rather than checking for it.
    """

    id: str
    """Stable name, used to select a probe and to attribute an observation."""

    def supports(self, target: str) -> bool:
        """Can this probe look at *target*? No side effects, no network."""
        ...

    async def observe(self, target: str, *, hint: str = "") -> Observation:
        """Look, and describe what is there.

        *hint* is what the caller is trying to find out — a probe may use it to
        decide what to include, and may ignore it entirely. Raises
        :class:`ProbeError` when it cannot look; never returns a plausible
        answer it did not get.
        """
        ...
