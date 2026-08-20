"""Phase 13 §7.1 — the acid test. One run, every link.

Modelled on ``tests/test_host_integration.py``, which is "the phase's acid test
rather than a demo". The value is in the joins, not the parts: each piece below
has its own unit test and passing all of those individually proved nothing about
whether a booking flow can actually park on a person and come back.

    navigate (durable)
      → tier 0 fills the form, no model call
      → TaintBroker REFUSES the submit                (read → write, unapproved)
      → the run parks on a human, who is handed a live-view URL
      → answered over the host's own channel
      → resumes and REATTACHES the same session       (not a fresh browser)
      → submits
      → replays: no clicks, no model calls, no new session
      → the journal is identical to the run that happened

Offline throughout: ``FakeBrowserProvider(durable=True)`` stands in for a hosted
browser that keeps sessions between processes.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, workflow
from loom.browser import FakeBrowserProvider, PageSnapshot, TreeNode
from loom.nodes.human.channel import DeliveryReceipt
from loom.runtime.effects import GuardedBroker
from loom.runtime.taint import TaintBroker, TaintPolicy
from loom.stores.memory import MemoryStore

BOOKING = PageSnapshot(
    url="https://fixture.test/reserve",
    title="Reserve a table",
    tree=(
        TreeNode(role="textbox", name="Full name"),
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="button", name="Confirm booking"),
    ),
    text="Reserve a table at the Fixture",
)


class RecordingChannel:
    """The host's own notification transport, as a test double.

    LOOM owns parking the run, journaling the request and validating the
    answer; delivering it to a person is the host's. This one records what it
    was handed, which is how the live-view URL is asserted.
    """

    name = "recording"

    def __init__(self) -> None:
        self.delivered: list = []
        self.withdrawn: list[str] = []

    async def deliver(self, request) -> DeliveryReceipt:
        self.delivered.append(request)
        return DeliveryReceipt(channel=self.name, delivered=True,
                               reference=f"ref-{len(self.delivered)}")

    async def withdraw(self, request_id: str, reference: str = "") -> None:
        self.withdrawn.append(request_id)


@workflow(name="book_a_table")
async def book_a_table(ctx: Context, booking: dict) -> str:
    """Fill a reservation, get a person to approve it, then submit."""
    page = await ctx.node("browser.navigate",
                          {"url": BOOKING.url, "scope": "durable"})
    session = page.session

    for field, value in (("Full name", booking["name"]),
                         ("Email", booking["email"])):
        await ctx.node("browser.act", {
            "session": session,
            "method": "fill",
            "target": {"role": "textbox", "name": field},
            "value": value,
            "effect": "read"})

    decision = await ctx.node("human.approval", {
        "subject": "booking",
        "prompt": f"Confirm the table for {booking['name']}?",
        # The takeover link, threaded from the browser rather than discovered.
        "live_view_url": session.live_view_url if session else ""})
    if not decision.approved:
        await ctx.node("browser.close", {"session": session})
        return "DECLINED"

    await ctx.node("browser.act", {
        "session": session,
        "method": "click",
        "target": {"role": "button", "name": "Confirm booking"},
        "effect": "write"})
    await ctx.node("browser.close", {"session": session})
    return f"BOOKED for {booking['name']}"


@pytest.fixture
def host():
    provider = FakeBrowserProvider({BOOKING.url: BOOKING},
                                   permissive=False, durable=True)
    channel = RecordingChannel()
    runtime = Runtime(
        store=MemoryStore(),
        browser=provider,
        human=channel,
        # The narrow dial: after reading a page you may write, but not delete.
        # The submit is still refused until approved, because the *taint* is
        # what governs it and only a person clears that.
        broker=TaintBroker(GuardedBroker(), TaintPolicy()),
    )
    return runtime, provider, channel


def performed(provider) -> list[tuple[str, str]]:
    return [(p.method.value, p.target.name)
            for s in provider.sessions for p in s.performed]


class TestTheAcidTest:
    async def test_one_run_start_to_finish(self, host) -> None:
        runtime, provider, channel = host
        payload = {"name": "Ada Lovelace", "email": "ada@example.com"}

        # --- the run parks on a person, having filled but not submitted -----
        parked = await runtime.run(book_a_table, payload)
        assert parked.status.value == "suspended", parked.error
        assert performed(provider) == [("fill", "Full name"), ("fill", "Email")], (
            "the submit must not have happened before anyone approved it")

        # --- the person was handed a way to see the browser -----------------
        assert len(channel.delivered) == 1
        request = channel.delivered[0]
        assert request.subject == "booking"
        assert request.live_view_url.startswith("https://fake.test/live/"), (
            "a durable session must reach the person as a takeover link — "
            "without it, 'approve this' is a question about a page they "
            "cannot see")

        # --- the browser survived the park ---------------------------------
        assert len(runtime.browser_sessions) == 0, (
            "the body released its hold when it parked")
        assert not provider.sessions[0].closed, (
            "a DURABLE session must NOT be closed when the body exits — the "
            "whole point of the scope is that it is still there afterwards")

        # --- answered over the host's own channel --------------------------
        await runtime.approve(parked.run_id, "booking")
        resumed = await runtime.resume(parked.run_id)
        assert resumed.status.value == "completed", resumed.error
        assert resumed.output == "BOOKED for Ada Lovelace"

        # --- it reattached rather than opening a fresh browser --------------
        assert len(provider.sessions) == 1, (
            "a second session means the run resumed against a browser that "
            "never saw its earlier steps — the failure that looks like success")
        assert performed(provider) == [
            ("fill", "Full name"), ("fill", "Email"), ("click", "Confirm booking")]

        # --- replay: nothing resolved, nothing clicked, nothing opened ------
        actions_before = len(performed(provider))
        sessions_before = len(provider.sessions)
        journal_before = [
            (e.path, e.name, e.status.value)
            for e in await runtime.store.load_journal(resumed.run_id)]

        replayed = await runtime.replay(resumed.run_id)

        assert replayed.output == resumed.output
        assert len(performed(provider)) == actions_before, "replay re-clicked"
        assert len(provider.sessions) == sessions_before, "replay opened a browser"
        assert len(channel.delivered) == 1, "replay re-notified a person"

        journal_after = [
            (e.path, e.name, e.status.value)
            for e in await runtime.store.load_journal(resumed.run_id)]
        assert journal_after == journal_before, "a replay rewrote the journal"

    async def test_a_rejection_closes_the_browser_and_books_nothing(
        self, host
    ) -> None:
        runtime, provider, _channel = host
        parked = await runtime.run(
            book_a_table, {"name": "Ada", "email": "a@example.com"})
        assert parked.status.value == "suspended"

        await runtime.approve(parked.run_id, "booking", approved=False)
        resumed = await runtime.resume(parked.run_id)

        assert resumed.output == "DECLINED"
        assert ("click", "Confirm booking") not in performed(provider)
        assert provider.sessions[0].closed, "a finished flow releases its browser"


class TestWhatItWouldTakeToBreakIt:
    """Each link removed, to show the test is load-bearing rather than lucky."""

    async def test_without_a_durable_scope_the_resume_refuses(self) -> None:
        """13.1/13.2's limitation, and the reason 13.3 exists.

        Identical workflow but a STEP session: the approval clears the taint,
        the browser died with the park, and ``browser.act`` refuses rather than
        driving a fresh page.
        """
        provider = FakeBrowserProvider({BOOKING.url: BOOKING}, permissive=False)
        runtime = Runtime(store=MemoryStore(), browser=provider,
                          human=RecordingChannel(),
                          broker=TaintBroker(GuardedBroker(), TaintPolicy()))

        @workflow(name="step_scoped_booking")
        async def flow(ctx: Context, _input) -> str:
            page = await ctx.node("browser.navigate", {"url": BOOKING.url})
            await ctx.node("human.approval", {"subject": "booking"})
            await ctx.node("browser.act", {
                "session": page.session,
                "method": "click",
                "target": {"role": "button", "name": "Confirm booking"},
                "effect": "write"})
            return "BOOKED"

        parked = await runtime.run(flow, None)
        assert parked.status.value == "suspended"
        await runtime.approve(parked.run_id, "booking")
        resumed = await runtime.resume(parked.run_id)

        assert resumed.status.value == "failed"
        assert resumed.error is not None
        assert resumed.error.type == "SessionLost"
        assert "did not outlive the process" in resumed.error.message

    async def test_a_provider_that_cannot_reattach_refuses_the_scope(self) -> None:
        """Refused at open, not at the first act.

        ``ExecutionSandbox.enforces``, one layer out: a host told "not here" is
        better off than one that believes its two-hour approval will survive.
        """
        provider = FakeBrowserProvider({BOOKING.url: BOOKING}, permissive=False)
        runtime = Runtime(store=MemoryStore(), browser=provider)

        @workflow(name="durable_on_a_step_provider")
        async def flow(ctx: Context, _input) -> str:
            await ctx.node("browser.navigate",
                           {"url": BOOKING.url, "scope": "durable"})
            return "opened"

        result = await runtime.run(flow, None)
        assert result.status.value == "failed"
        assert result.error is not None
        assert "DURABLE" in result.error.message
        assert performed(provider) == []
