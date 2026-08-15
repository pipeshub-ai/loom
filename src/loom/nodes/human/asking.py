"""The one mechanism every ``human.*`` node is built on.

``human.form`` is the general case; approval, choice, and review are it with a
fixed response model and a better name. That is deliberate — one mechanism, and
the specific nodes exist because a catalog entry named ``approval`` is findable
while ``form(response_model=Approval)`` is not.

Two properties are load-bearing and both come from the journal:

**Delivery happens exactly once per request across replays.** It runs inside a
durable call, so an engine restart re-reads the receipt instead of re-notifying.
Without that a person gets the same approval request once per crash, which is
the fastest way to make a team ignore the channel.

**The request itself is journaled**, as the delivery call's output. That is what
lets ``loom pending`` answer "which runs are waiting on a person, and for what"
without a second store to keep in step with the journal — the run is suspended
on ``approval:<subject>`` and the sibling entry holds the request.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from loom.core.types import to_seconds
from loom.nodes.base import NodeContext
from loom.nodes.human.channel import DeliveryReceipt, HumanRequest
from loom.nodes.spec import NodeDuration

logger = logging.getLogger(__name__)

__all__ = ["HumanTicket", "TimeoutPolicy", "ask", "event_name"]

#: Marks a wait that ended on the clock rather than on an answer. Journaled as
#: the event's default, so a replay sees the timeout exactly where the original
#: run did instead of re-deciding it against a clock that has since moved.
_TIMED_OUT = {"__loom_timed_out__": True}


class TimeoutPolicy:
    """What an unanswered request means. Named, because ``str`` is not a policy."""

    REJECT = "reject"
    APPROVE = "approve"
    FAIL = "fail"
    ESCALATE = "escalate"


class HumanTicket(BaseModel):
    """The journaled record of one request having been raised.

    Carries the request as well as the receipt: the receipt says what the
    channel did, and the request is what anybody looking at a parked run needs
    in order to answer it.
    """

    request: HumanRequest
    receipt: DeliveryReceipt = Field(default_factory=DeliveryReceipt)


def event_name(subject: str) -> str:
    """The event that answers a request about *subject*.

    ``approval:<subject>``, unchanged from ``ctx.wait_for_approval`` — so
    ``runtime.approve(run_id, subject)`` resolves a request raised by a node and
    one raised by the older call in exactly the same way, and a workflow written
    before nodes existed keeps working.
    """
    return f"approval:{subject}"


async def ask(
    ctx: NodeContext,
    *,
    node_id: str,
    subject: str,
    prompt: str,
    context: dict[str, Any] | None = None,
    assignees: list[str] | None = None,
    response_schema: dict[str, Any] | None = None,
    timeout: NodeDuration | None = None,
) -> tuple[Any, bool, HumanTicket]:
    """Raise a request, park, and return ``(answer, timed_out, ticket)``.

    The answer is whatever the responder sent, unvalidated — each node validates
    it against its own ``Output``, because only the node knows what shape it
    asked for.
    """
    channel = ctx.capability("human_channel")

    request_id = ctx.uuid4()
    created_at = ctx.now()
    expires_at = (
        created_at + timedelta(seconds=to_seconds(timeout)) if timeout else None
    )
    request = HumanRequest(
        request_id=request_id,
        run_id=ctx.run_id,
        workflow=ctx.workflow,
        node_id=node_id,
        subject=subject,
        prompt=prompt,
        context=context or {},
        response_schema=response_schema or {},
        assignees=assignees or [],
        expires_at=expires_at,
        created_at=created_at,
    )

    async def deliver() -> HumanTicket:
        receipt = await channel.deliver(request)
        if not isinstance(receipt, DeliveryReceipt):
            receipt = DeliveryReceipt.model_validate(receipt or {})
        if not receipt.channel:
            receipt = receipt.model_copy(
                update={"channel": getattr(channel, "name", type(channel).__name__)}
            )
        return HumanTicket(request=request, receipt=receipt)

    ticket: HumanTicket = await ctx.call(f"deliver:{subject}", deliver)

    answer = await ctx.wait_for_event(
        event_name(subject), timeout=timeout, default=_TIMED_OUT
    )

    if _is_timeout(answer):
        await _withdraw(ctx, channel, request_id, subject, "the request expired")
        return None, True, ticket
    return answer, False, ticket


def _is_timeout(answer: Any) -> bool:
    return isinstance(answer, dict) and answer.get("__loom_timed_out__") is True


async def _withdraw(
    ctx: NodeContext, channel: Any, request_id: str, subject: str, reason: str
) -> None:
    """Tell the channel to stop offering an answer nobody will act on.

    Best effort, and journaled so it happens once. A failure is recorded and
    swallowed: the run's outcome was already decided by the timeout, and letting
    a Slack API hiccup change it would make the outcome depend on the notifier.
    """

    async def perform() -> str:
        try:
            await channel.withdraw(request_id, reason)
        except Exception as exc:
            logger.warning(
                "withdrawing human request %s failed: %s", request_id, exc, exc_info=True
            )
            return f"failed: {type(exc).__name__}: {exc}"
        return "withdrawn"

    await ctx.call(f"withdraw:{subject}", perform)
