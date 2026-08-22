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

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from loom.blobs.attachment import Attachment

__all__ = [
    "Observation",
    "ObservedPage",
    "Probe",
    "ProbeError",
    "control_names",
    "redirect_note",
]


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
    landed: str = ""
    """Where the look actually ended up, when that differs from *target*.

    Empty means "the same place", which is what every probe written before this
    reports and what the overwhelming majority of looks are.

    Carried because a probe otherwise cannot tell a model the one thing that
    invalidates everything else it says. A redirect produces a perfectly clean
    census of a page nobody asked about, and there is no signal in a healthy
    census that separates *"this page is simple"* from *"this is not the page
    you asked for"*. That is the founding failure of this package — "a workflow
    that ran, completed, and answered wrongly looked exactly like one that
    worked" — occurring one level inward, in the instrument built to prevent it.

    Observed: ``/reserve`` redirected to ``/reserve/message``, an interstitial
    notice. The probe reported five controls and ``5/5 addressable by name``,
    the sentence that decides whether role-and-name can drive a page at all, and
    the agent wrote a workflow against a page it had never seen.
    """
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


@dataclass(frozen=True)
class ObservedPage:
    """One page the agent actually looked at, kept for the checks to use.

    The census was already being produced, handed to the model, and then
    dropped. So nothing downstream could answer the cheapest question there is
    about a browser workflow — *does the control this code addresses exist on
    the page the agent saw?* — and the run that motivated this addressed a party
    size control on a page whose only controls were a nav button and a notice.
    """

    target: str
    landed: str
    names: tuple[str, ...]
    """Accessible names the census reported, in the order it reported them."""

    @property
    def redirected(self) -> bool:
        return bool(self.landed) and self.landed != self.target


def control_names(detail: str) -> tuple[str, ...]:
    """Every name a census names a control by. Empty for anything else.

    Tolerant on purpose: it is handed whatever a probe put in ``detail``, which
    for an HTTP probe is not a census at all. A check built on this must degrade
    to saying nothing rather than to a finding, because "I could not read that"
    and "the control is missing" are opposite conclusions.
    """
    try:
        census = json.loads(detail)
    except (ValueError, TypeError):
        return ()
    if not isinstance(census, dict):
        return ()

    found: list[str] = []
    for node in census.get("tree") or ():
        if isinstance(node, dict) and (node.get("name") or "").strip():
            found.append(str(node["name"]).strip())
    for key in ("native_controls", "role_widgets"):
        for node in census.get(key) or ():
            if isinstance(node, dict) and (node.get("label") or "").strip():
                found.append(str(node["label"]).strip())
    for label in census.get("buttons") or ():
        if isinstance(label, str) and label.strip():
            found.append(label.strip())

    seen: dict[str, None] = {}
    for name in found:
        seen.setdefault(name, None)
    return tuple(seen)


def redirect_note(target: str, landed: str) -> str:
    """The sentence that goes *first*, when a look did not stay where it was sent.

    Leading rather than appended, and in the summary rather than only in the
    structured detail, because the summary is what a model reads before it
    decides whether it has what it needs. A redirect discovered after the census
    has already been believed is a redirect discovered too late.

    Compared on the whole URL deliberately. A trailing slash or a query string
    the server added is still somewhere else, and a probe that quietly decides
    which differences are unimportant is making exactly the judgement the caller
    is better placed to make.
    """
    if not landed or landed == target:
        return ""
    return (
        f"REDIRECTED — you asked about {target} and this describes {landed}. "
        "Everything below is that second page. If the controls you expected are "
        "missing, they are most likely still on the other side of this one."
    )


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
