"""Human-in-the-loop nodes, and the provider interface they park on.

LOOM owns parking the run and validating the answer. Delivering the request to
a person is the provider's — implement :class:`HumanChannel` and pass it as
``Runtime(human=...)``.

    from loom.nodes.human import ApprovalIn, ConsoleChannel

    rt = Runtime(store=MemoryStore(), human=ConsoleChannel())

    @workflow
    async def refund(ctx, order):
        decision = await ctx.node("human.approval", ApprovalIn(
            subject=f"refund-{order.id}",
            prompt=f"Approve a ${order.amount} refund?",
            timeout="24h",
        ))
        if not decision.approved:
            return "held"

Without a channel a ``human.*`` node raises before the run parks, rather than
parking with nobody listening — which is indistinguishable from patience.
"""

from __future__ import annotations

from loom.nodes.human.asking import (
    HumanTicket,
    TimeoutPolicy,
    ask,
    event_name,
)
from loom.nodes.human.channel import (
    DeliveryReceipt,
    HumanChannel,
    HumanRequest,
)
from loom.nodes.human.channels import (
    AutoRespondChannel,
    ConsoleChannel,
    LogChannel,
    WebhookChannel,
)
from loom.nodes.human.nodes import (
    ApprovalIn,
    ApprovalNode,
    ApprovalOut,
    ChoiceIn,
    ChoiceNode,
    ChoiceOut,
    EscalateIn,
    EscalateNode,
    EscalateOut,
    FormIn,
    FormNode,
    FormOut,
    ReviewIn,
    ReviewNode,
    ReviewOut,
)

__all__ = [
    "ApprovalIn",
    "ApprovalNode",
    "ApprovalOut",
    "AutoRespondChannel",
    "ChoiceIn",
    "ChoiceNode",
    "ChoiceOut",
    "ConsoleChannel",
    "DeliveryReceipt",
    "EscalateIn",
    "EscalateNode",
    "EscalateOut",
    "FormIn",
    "FormNode",
    "FormOut",
    "HumanChannel",
    "HumanRequest",
    "HumanTicket",
    "LogChannel",
    "ReviewIn",
    "ReviewNode",
    "ReviewOut",
    "TimeoutPolicy",
    "WebhookChannel",
    "ask",
    "event_name",
]
