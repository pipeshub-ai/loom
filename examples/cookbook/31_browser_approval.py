"""A browser flow that stops and asks a person before it commits.

The shape most real automations need and the one that is hardest to get right:
fill the form, then have somebody look at it before anything is submitted — with
the run costing nothing while it waits, and the browser still there when they
answer.

Runs offline against ``FakeBrowserProvider(durable=True)``, which stands in for
a hosted browser that keeps sessions between processes. Swap in a real provider
whose ``supports()`` includes ``reattach`` and the workflow is unchanged.

Four things make this work, and none of them is browser-specific:

1. **``scope="durable"``.** Under the default ``STEP`` scope the browser dies
   when the body exits — and parking on a person *is* the body exiting. A
   durable session is released rather than closed, so it survives the wait.
2. **The session is a value you carry.** ``page.session`` comes back from
   ``navigate``, so on resume it is served from the journal like any other
   recorded value and the run reattaches to the browser it actually had.
3. **The effect declaration does the refusing.** Reading the page taints the
   run; the submit is declared ``write``; ``TaintBroker`` refuses it until a
   person has said yes. No browser-specific policy anywhere.
4. **The person is handed a live view.** "Approve this" is not a fair question
   about a page they cannot see.

Run it and watch the order: the form is filled, nothing is submitted, the run
suspends, and only after the approval does the click happen — against the same
browser.
"""

from __future__ import annotations

from loom import Context, Runtime, workflow
from loom.browser import FakeBrowserProvider, PageSnapshot, TreeNode
from loom.nodes.human.channel import DeliveryReceipt
from loom.runtime.effects import GuardedBroker
from loom.runtime.shutdown import run_main
from loom.runtime.taint import TaintBroker, TaintPolicy
from loom.stores.memory import MemoryStore

RESERVATION = PageSnapshot(
    url="https://fixture.test/reserve",
    title="Reserve a table",
    tree=(
        TreeNode(role="textbox", name="Full name"),
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="combobox", name="Party size"),
        TreeNode(role="button", name="Confirm booking"),
    ),
    text="Reserve a table. Bookings open daily at 10am.",
)


class ConsoleChannel:
    """The host's notification transport.

    LOOM owns parking the run, journaling the request and validating the
    answer. **Delivering it to a person is yours** — a Slack message, an email,
    a row in your own table. This one prints, which is the smallest honest
    implementation of the same contract.
    """

    name = "console"

    async def deliver(self, request) -> DeliveryReceipt:
        print(f"\n  [{self.name}] {request.prompt}")
        for key, value in request.context.items():
            print(f"      {key}: {value}")
        if request.live_view_url:
            print(f"      watch or take over: {request.live_view_url}")
        return DeliveryReceipt(channel=self.name, delivered=True,
                               reference=request.request_id)

    async def withdraw(self, request_id: str, reference: str = "") -> None:
        print(f"  [{self.name}] withdrew {request_id}")


@workflow(name="reserve_with_approval")
async def reserve_with_approval(ctx: Context, booking: dict) -> str:
    page = await ctx.node("browser.navigate", {
        "url": RESERVATION.url,
        # Without this the browser would not survive the approval below.
        "scope": "durable",
    })
    session = page.session

    for field, value in (("Full name", booking["name"]),
                         ("Email", booking["email"])):
        await ctx.node("browser.act", {
            "session": session,
            "method": "fill",
            "target": {"role": "textbox", "name": field},
            "value": value,
            "effect": "read",          # typing changes nothing outside the page
        })

    decision = await ctx.node("human.approval", {
        "subject": "booking",
        "prompt": f"Confirm the table for {booking['name']}?",
        "context": {"name": booking["name"], "email": booking["email"]},
        "live_view_url": session.live_view_url if session else "",
    })

    if not decision.approved:
        await ctx.node("browser.close", {"session": session})
        return "declined — nothing was submitted"

    # Reached only after a person said yes. The taint the page read created is
    # cleared by their approval, so this write is allowed; without it the
    # broker refuses here and the booking never happens.
    await ctx.node("browser.act", {
        "session": session,
        "method": "click",
        "target": {"role": "button", "name": "Confirm booking"},
        "effect": "write",
    })
    await ctx.node("browser.close", {"session": session})
    return f"booked for {booking['name']}"


async def main() -> None:
    provider = FakeBrowserProvider({RESERVATION.url: RESERVATION},
                                   permissive=False, durable=True)
    async with Runtime(
        store=MemoryStore(),
        browser=provider,
        human=ConsoleChannel(),
        broker=TaintBroker(GuardedBroker(), TaintPolicy()),
    ) as rt:
        booking = {"name": "Ada Lovelace", "email": "ada@example.com"}

        parked = await rt.run(reserve_with_approval, booking)
        print(f"\nStatus: {parked.status.value}  (exit code 3 — parked on a "
              f"person, neither succeeded nor failed)")
        print("Actions so far:",
              [(p.method.value, p.target.name)
               for s in provider.sessions for p in s.performed])
        print("Browser still open:", not provider.sessions[0].closed)

        # In production this arrives from your channel — a Slack button, an
        # HTTP call to POST /runs/{run}/events, `loom approve <run> booking`.
        await rt.approve(parked.run_id, "booking")
        done = await rt.resume(parked.run_id)

        print(f"\nStatus: {done.status.value}")
        print(f"Output: {done.output}")
        print("Actions in total:",
              [(p.method.value, p.target.name)
               for s in provider.sessions for p in s.performed])
        print("Sessions opened:", len(provider.sessions),
              "(one — it reattached rather than starting over)")


if __name__ == "__main__":
    run_main(main())
