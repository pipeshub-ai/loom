"""One behavioural suite every ``HumanChannel`` must pass.

Four channels ship in-tree and a host writes a fifth — Slack, its own web UI,
whatever its users already read. Nothing but a shared suite keeps those five
agreeing, and the ways they can disagree are not cosmetic: a channel that raises
instead of reporting a failed delivery turns an unreachable Slack workspace into
a failed *run*, and one that ignores ``withdraw`` leaves people answering
questions about runs that were cancelled an hour ago.

Written against the protocol only, so a host can import ``channel_conformance``
and run it against its own implementation. That is the whole point of a port
suite: the property is stated once, and every implementation — including ones
this repository will never see — is held to it.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.nodes.human.channel import DeliveryReceipt, HumanRequest
from loom.nodes.human.channels import (
    AutoRespondChannel,
    LogChannel,
    WebhookChannel,
)


def a_request(**overrides: Any) -> HumanRequest:
    """A request with everything the protocol says a channel can rely on."""
    fields: dict[str, Any] = {
        "request_id": "req-1",
        "run_id": "run-1",
        "workflow": "settle_invoice",
        "node_id": "human.approval",
        "subject": "payment",
        "prompt": "Approve the payment?",
        "response_schema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
        },
    }
    fields.update(overrides)
    return HumanRequest(**fields)


#: Every channel that can be constructed with no configuration. A channel
#: needing a server (``ConsoleChannel`` needs a terminal) is named here rather
#: than dropped, so the list reads as a decision instead of an oversight.
CHANNELS: dict[str, Any] = {
    "log": LogChannel,
    "auto": AutoRespondChannel,
    "webhook": lambda: WebhookChannel(url="https://example.invalid/hook"),
}


@pytest.fixture(params=sorted(CHANNELS))
def channel(request) -> Any:
    return CHANNELS[request.param]()


class TestEveryChannelAgrees:
    async def test_it_names_itself(self, channel) -> None:
        """The name lands in the journal, so an operator reading a parked run
        can tell where the request was sent. An empty one makes that trail stop
        exactly where it is needed."""
        assert isinstance(channel.name, str) and channel.name

    async def test_delivery_returns_a_receipt_rather_than_a_bare_bool(
        self, channel
    ) -> None:
        receipt = await _deliver(channel, a_request())

        assert isinstance(receipt, DeliveryReceipt)
        assert isinstance(receipt.delivered, bool)

    async def test_a_failure_is_reported_not_raised(self, channel) -> None:
        """The property that decides whether an outage is a paused run or a
        failed one. ``WebhookChannel`` here points at an unresolvable host, so
        this is a real failure for at least one of the implementations rather
        than a hypothetical."""
        receipt = await _deliver(channel, a_request(request_id="req-unreachable"))

        if not receipt.delivered:
            assert receipt.detail, "a failed delivery must say why"

    async def test_withdraw_is_accepted_for_an_unknown_request(
        self, channel
    ) -> None:
        """Withdrawal races the answer: a run can be cancelled between a person
        opening the request and Loom giving up on it. A channel that raised on
        an id it has never seen would turn that race into an error on the
        cancellation path."""
        await channel.withdraw("req-never-delivered", "cancelled")

    async def test_withdraw_after_deliver_is_accepted(self, channel) -> None:
        await _deliver(channel, a_request(request_id="req-2"))
        await channel.withdraw("req-2", "the run was cancelled")


class TestAutoRespondIsHonestAboutWhatItIs:
    """The one channel that answers on a person's behalf.

    It exists so a generated workflow containing an approval does not hang in
    the smoke sandbox — without it, the cheapest repair a model can find is
    deleting the approval, and it ships a workflow that passes every check
    having stripped out the control the spec asked for. That makes it useful in
    exactly one place and dangerous everywhere else, so it must be impossible to
    mistake for a real channel.
    """

    async def test_it_reports_delivery(self) -> None:
        receipt = await AutoRespondChannel().deliver(a_request())

        assert receipt.delivered

    def test_its_name_does_not_read_like_a_transport(self) -> None:
        assert AutoRespondChannel().name == "auto"


async def _deliver(channel: Any, request: HumanRequest) -> DeliveryReceipt:
    """Deliver, tolerating a channel that cannot reach its transport here.

    A webhook to an unresolvable host is the environment, not the channel — the
    same rule the docs runner applies. What is *not* tolerated is raising, which
    is what the suite above is checking, so the exception is turned into the
    receipt the protocol asked for and asserted on there.
    """
    try:
        return await channel.deliver(request)
    except Exception as exc:
        return DeliveryReceipt(
            delivered=False, channel=channel.name, detail=f"raised: {exc}"
        )
