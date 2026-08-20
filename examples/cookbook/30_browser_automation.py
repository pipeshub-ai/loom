"""Driving a web page from a workflow — phase 13.1, tier 0.

Runs offline against ``FakeBrowserProvider``, so this file is executable
documentation rather than a snippet nobody checks. Point ``Runtime`` at
``LocalBrowserProvider()`` and change the URL and it drives a real Chromium
unchanged — that is the whole value of the port.

Four things worth reading for, all visible below:

1. **A control is addressed by what a person reads**, never by a CSS selector.
   ``Target(role="textbox", name="Email")`` resolves through the accessible
   name, then placeholder, then label — the chain ``tests/corpus`` measured at
   76% across 114 controls on 10 real pages.
2. **Ambiguity raises.** Two matching controls is not a coin toss, it is a
   question the caller has to answer with ``ordinal``. Picking one is how an
   automation clicks the wrong button and reports success.
3. **A replay clicks nothing.** Every browser call is a journal entry, so
   re-running a settled flow resolves nothing and performs nothing.
4. **The session belongs to the body**, not to you. It opens on the first
   navigate and closes however the body exits, including on failure.
5. **Every action declares what it does to the world.** That one field is what
   lets ``TaintBroker`` refuse a submit on a run that has read a page, with no
   browser-specific policy anywhere. WRITE is the default because a read left at
   the default is merely refused more often than it needs to be, while a write
   mistaken for a read is a booking nobody approved.
"""

from __future__ import annotations

from loom import Context, Runtime, workflow
from loom.browser import FakeBrowserProvider, PageSnapshot, TreeNode
from loom.runtime.shutdown import run_main
from loom.stores.memory import MemoryStore

#: Stands in for a real page. With LocalBrowserProvider this is the site.
BOOKING_PAGE = PageSnapshot(
    url="https://fixture.test/reserve",
    title="Reserve a table",
    tree=(
        TreeNode(role="textbox", name="Full name"),
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="combobox", name="Party size"),
        TreeNode(role="button", name="Check availability"),
        # Deliberately repeated, as a real listing repeats a control:
        TreeNode(role="button", name="Notify me"),
        TreeNode(role="button", name="Notify me"),
    ),
    text="Reserve a table at the Fixture. Booking opens daily at 10am.",
)


@workflow(name="reserve_table")
async def reserve_table(ctx: Context, booking: dict) -> str:
    """Fill a reservation form and read back what the page says."""
    page = await ctx.node("browser.navigate", {"url": BOOKING_PAGE.url})
    await ctx.report(f"opened {page.title}: {page.summary}")

    # Each of these is its own journal entry. A crash between them re-runs the
    # body, serves these from the journal, and — because a fresh browser is not
    # where the flow left off — browser.act refuses rather than driving a blank
    # page. Wrap the flow in one ctx.step if you would rather it re-ran whole.
    for field, value in (("Full name", booking["name"]),
                         ("Email", booking["email"])):
        await ctx.node("browser.act", {
            "method": "fill",
            "target": {"role": "textbox", "name": field},
            "value": value,
            # Typing into a field changes nothing outside this page.
            "effect": "read",
        })

    await ctx.node("browser.act", {
        "method": "click",
        "target": {"role": "button", "name": "Check availability"},
        "effect": "read",
        # If this button is renamed, re-aim from the description rather than
        # failing — safe here precisely because it is declared a read.
        "intent": "the button that searches for available tables",
    })

    # A page that repeats a control needs the caller to say which one. Without
    # `ordinal` this raises AmbiguousTarget instead of guessing.
    await ctx.node("browser.act", {
        "method": "click",
        "target": {"role": "button", "name": "Notify me", "ordinal": 1},
        # A write. Declared, so a Runtime with a TaintBroker refuses it unless a
        # person has approved — and so it is never silently re-aimed if the page
        # changes underneath it.
        "effect": "write",
    })

    text = await ctx.node("browser.extract", {})
    return f"{page.title} — {len(page.controls)} controls. Page says: {text.text}"


async def main() -> None:
    provider = FakeBrowserProvider({BOOKING_PAGE.url: BOOKING_PAGE},
                                   permissive=False)
    # Swap this one line for LocalBrowserProvider() to drive a real browser:
    #     from loom.browser import LocalBrowserProvider
    #     browser = LocalBrowserProvider()
    async with Runtime(store=MemoryStore(), browser=provider) as rt:
        result = await rt.run(
            reserve_table,
            {"name": "Ada Lovelace", "email": "ada@example.com"},
        )
        print(f"Status: {result.status.value}")
        print(f"Output: {result.output}")

        performed = [(p.method.value, p.target.name)
                     for s in provider.sessions for p in s.performed]
        print(f"Browser actions performed: {performed}")

        # The journal is the action cache. Replaying resolves nothing and
        # clicks nothing — no bespoke caching layer involved.
        replayed = await rt.replay(result.run_id)
        after = sum(len(s.performed) for s in provider.sessions)
        print(f"Replay status: {replayed.status.value}")
        print(f"Actions after replay: {after} (unchanged — the journal served it)")


if __name__ == "__main__":
    run_main(main())
