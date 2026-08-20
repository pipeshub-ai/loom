"""The provider boundary for human-in-the-loop.

The split is the whole design::

    LOOM owns                        The provider owns
    ---------                        -----------------
    parking the run (Suspend)        delivering the request to a person
    journaling the request           rendering it (Slack blocks, email, a form)
    validating the response          collecting the answer
    resuming, typed                  the identity of the responder
    expiry and escalation policy

``ctx.wait_for_approval`` already had the LOOM half right — a parked run costs
nothing, and the answer is journaled. What it had no answer for is that
**nobody is told**: the request existed only as a journal entry someone had to
go looking for, which in practice is discovered a day late.

A :class:`HumanChannel` closes that, and closes it *outside* the engine. A
provider renders :class:`HumanRequest` — which carries the JSON Schema of the
answer LOOM will accept — so a Slack implementation builds blocks from it, a web
implementation builds a form from it, and neither needs to know anything about
journals or replay.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "DeliveryReceipt",
    "HumanChannel",
    "HumanRequest",
]


class HumanRequest(BaseModel):
    """What a person is being asked, and what shape the answer must take."""

    request_id: str
    run_id: str
    workflow: str = ""
    node_id: str
    subject: str
    """Names the decision within the run. The event that resolves it is
    ``approval:<subject>``, which is what ``runtime.approve(run_id, subject)``
    sends — so a request raised by a node and one raised by the older
    ``ctx.wait_for_approval`` are answered the same way."""

    prompt: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    """What the person needs in order to decide. Rendered by the channel."""

    response_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema of the accepted answer. A channel builds its UI from this
    rather than hard-coding one shape per node kind."""

    assignees: list[str] = Field(default_factory=list)
    """Who is being asked. Whether they are *authenticated* is the channel's
    business — LOOM records the responder the channel reports and claims nothing
    more about it than that."""

    live_view_url: str = ""
    """Where a person can watch — or take over — the browser this run is
    holding, when it is holding one.

    Typed rather than left in :attr:`context` because a channel builds its UI
    from the request's shape, and a takeover link it has to discover by
    guessing a dict key is one no channel will offer. Empty whenever the run
    has no durable browser, which is the common case.

    This is the whole reason ``SessionScope.DURABLE`` exists: a run parked on a
    2FA prompt costs nothing while it waits, but the *browser* has to still be
    there when the person finishes."""

    expires_at: datetime | None = None
    created_at: datetime | None = None


class DeliveryReceipt(BaseModel):
    """What the channel did with a request.

    Journaled, so a replay does not re-deliver. ``delivered=False`` with a reason
    is a legitimate outcome — a channel that only records requests reports
    exactly that, rather than claiming a notification it never sent.
    """

    channel: str = ""
    delivered: bool = True
    reference: str = ""
    """The channel's own handle for the message — a Slack ts, a ticket id —
    which is what :meth:`HumanChannel.withdraw` needs later."""
    detail: str = ""


@runtime_checkable
class HumanChannel(Protocol):
    """How a request reaches a person. Implemented by the provider.

    Two methods, because there are two events: the request is raised, and it
    stops being answerable. ``withdraw`` is what keeps a Slack message from
    staying clickable after the run was cancelled or the deadline passed.
    """

    @property
    def name(self) -> str: ...

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        """Put *request* in front of its assignees.

        Called through ``ctx.step``, so it is journaled and runs **exactly once
        per request across replays**. Without that, every engine restart would
        re-notify and a person would get the same approval five times.
        """
        ...

    async def withdraw(self, request_id: str, reason: str) -> None:
        """The request is no longer answerable.

        Best effort. A failure here is logged and never masks the run's outcome
        — the same rule compensation handlers follow.
        """
        ...
