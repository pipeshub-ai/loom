# Driving a web page

For the services that publish a page and no API — which is most of the long
tail. Reach for it **after** checking for an API: a browser is the slowest and
most fragile way to talk to anything, and every vendor that offers both prefers
you use the other one.

LOOM ships no browser infrastructure. It ships two Protocols, one reference
provider over Playwright, a fake for offline tests, and a conformance kit, so a
host can put Browserbase, Kernel or Anchor behind the same seam. Same position
`loom/knowledge/` takes about vector databases.

```bash
pip install 'loomsdk[browser]' && playwright install chromium
```

<!-- docs-preamble -->

```python
import asyncio

from loom import Context, Runtime, workflow
from loom.browser import (
    BrowserPolicy,
    FakeBrowserProvider,
    LocalBrowserProvider,
    PageSnapshot,
    SessionScope,
    Target,
    TreeNode,
)
from loom.runtime.effects import GuardedBroker
from loom.runtime.taint import TaintBroker, TaintPolicy
from loom.stores.memory import MemoryStore
```

## Reading a page

```python
@workflow(name="read_the_page")
async def read_the_page(ctx: Context, url: str) -> str:
    page = await ctx.node("browser.navigate", {"url": url})
    return page.summary
```

`browser.navigate` returns the page's controls, each with the role and
accessible name that address it. **Write your targets from what it reports**,
not from what the page probably contains — that is the whole reason it returns
them.

`page.summary` is one line, and it says the thing that decides whether this
approach works at all: how many controls carry a name. A page reporting `0
named` is not a page you need a better selector for, it is a page a
role-and-name target cannot drive.

## Acting on a control

Address it the way a person reads it:

```python
@workflow(name="fill_a_form")
async def fill_a_form(ctx: Context, form: dict) -> str:
    await ctx.node("browser.navigate", {"url": form["url"]})
    await ctx.node("browser.act", {
        "method": "fill",
        "target": {"role": "textbox", "name": "Email"},
        "value": form["email"],
        "effect": "read",
    })
    return "filled"
```

Resolution tries the accessible name, then the placeholder, then the label. The
middle one is not a nicety: `aria-label` **overrides** `placeholder` in
accessible-name computation, so a field showing `Your email` is often named
`Email`, and one showing `To?` is named `Destination location`. Measured across
114 hand-labelled controls on ten real pages, that chain resolves 76% with no
model call at all.

A CSS path or XPath goes in `css=`, beside a role and name, and only when
nothing else reaches the control. A selector read off a page while authoring is
right on that render and silently wrong afterwards — it matches *nothing*, so
the workflow completes and reports an empty result. The `selectors` check
reports one.

### Two matches raise

```python
@workflow(name="pick_one_of_many")
async def pick_one_of_many(ctx: Context, url: str) -> str:
    await ctx.node("browser.navigate", {"url": url})
    await ctx.node("browser.act", {
        "method": "click",
        "target": {"role": "button", "name": "Notify", "ordinal": 2},
        "effect": "read",
    })
    return "clicked the second one, deliberately"
```

Without `ordinal`, a target matching several controls raises `AmbiguousTarget`.
Refusing is the feature: picking one is how an automation acts on the wrong row
and reports success.

### When you cannot name it exactly

```python
@workflow(name="find_then_act")
async def find_then_act(ctx: Context, url: str) -> str:
    await ctx.node("browser.navigate", {"url": url})
    found = await ctx.node("browser.observe",
                           {"intent": "the button that submits the form"})
    if not found.found:
        return f"nothing matched: {found.reason}"
    await ctx.node("browser.act", {
        "method": "click", "target": found.target.model_dump(), "effect": "read"})
    return "clicked"
```

`browser.observe` returns a target **without acting**, so you can inspect it
first. It tries an exact name match before anything else and costs no model
call when that succeeds — `found.tier` says which answered.

## Declaring what an action does

`effect` is `read` for filling a field or moving between steps, `write` for
submitting, sending or confirming, `destructive` for cancelling or deleting.

It cannot be inferred. `click "Next"` and `click "Confirm booking"` are the same
shape, and a keyword list over the name would be a guess. The default is
`write`, which is a fail-safe backstop rather than a classification: a read left
at the default is merely refused more often than it needs to be, while a write
mistaken for a read is a booking nobody approved.

That one field is what lets the runtime refuse a submit on a run that has read a
page:

```python
def guarded_runtime(provider) -> Runtime:
    return Runtime(
        store=MemoryStore(),
        browser=provider,
        broker=TaintBroker(GuardedBroker(), TaintPolicy()),
    )
```

Navigating is an open-world read, so it taints the run; a `write` after it is
refused until a person approves. No browser-specific policy is involved —
`TaintBroker` reads the effect class and `open_world`, which the browser nodes
declare like any other node.

## Parking on a person

An approval parks the run, and a normal browser session ends when the body
exits — so the flow must be opened as durable, or the browser will not be there
when the person answers:

```python
@workflow(name="reserve_with_approval")
async def reserve_with_approval(ctx: Context, booking: dict) -> str:
    page = await ctx.node("browser.navigate",
                          {"url": booking["url"], "scope": "durable"})
    session = page.session

    await ctx.node("browser.act", {
        "session": session,
        "method": "fill",
        "target": {"role": "textbox", "name": "Email"},
        "value": booking["email"],
        "effect": "read",
    })

    decision = await ctx.node("human.approval", {
        "subject": "booking",
        "prompt": "Confirm this reservation?",
        "live_view_url": session.live_view_url if session else "",
    })
    if not decision.approved:
        await ctx.node("browser.close", {"session": session})
        return "declined"

    await ctx.node("browser.act", {
        "session": session,
        "method": "click",
        "target": {"role": "button", "name": "Confirm booking"},
        "effect": "write",
    })
    await ctx.node("browser.close", {"session": session})
    return "booked"
```

Three things are doing work there.

**`scope="durable"`** means the session outlives this execution of the body. A
provider that cannot honour it refuses at open rather than downgrading, because
a host that believes its two-hour approval will survive and is wrong finds out
at the worst moment.

**`session` passed to each call.** The ref comes back from `navigate`, so on
resume it is served from the journal like any other recorded value — that is how
the run reattaches to the browser it actually had, instead of a fresh one
somewhere else on the site. It also makes the dependency visible: code that acts
on a session it did not navigate reads as wrong.

**`live_view_url`** gives the person somewhere to watch, or to finish a 2FA
prompt themselves. "Approve this" is not a fair question about a page they
cannot see.

**`browser.close`** ends it. A durable session is deliberately not closed when
the body exits, so ending it is the workflow's decision — providers also expire
sessions on their own TTL, so a forgotten close is a cost rather than a leak.

## Testing without a browser

`FakeBrowserProvider` serves recorded pages. Nothing needs Chromium, the network
or the extra:

```python
PAGE = PageSnapshot(
    url="https://fixture.test/form",
    title="A form",
    tree=(
        TreeNode(role="textbox", name="Email"),
        TreeNode(role="button", name="Submit"),
    ),
)


@workflow(name="fill_the_fixture")
async def fill_the_fixture(ctx: Context, email: str) -> str:
    await ctx.node("browser.navigate", {"url": PAGE.url})
    await ctx.node("browser.act", {
        "method": "fill",
        "target": {"role": "textbox", "name": "Email"},
        "value": email,
        "effect": "read",
    })
    return "filled"


async def run_against_a_fake() -> str:
    provider = FakeBrowserProvider({PAGE.url: PAGE}, permissive=False)
    async with Runtime(store=MemoryStore(), browser=provider) as rt:
        result = await rt.run(fill_the_fixture, "someone@example.com")
        return str(result.output)
```

`permissive=False` enforces the full contract — strict matching, visibility,
a closed session that refuses — which is what a test wants. The default,
`permissive=True`, answers anything and is what the coding agent's smoke sandbox
uses, so that a generated browser workflow can be *run* somewhere with no network
rather than only reaching a connection error.

## Running it for real

```python
@workflow(name="summarise_a_page")
async def summarise_a_page(ctx: Context, url: str) -> str:
    page = await ctx.node("browser.navigate", {"url": url})
    return page.summary


async def run_for_real(url: str) -> str:
    async with Runtime(store=MemoryStore(),
                       browser=LocalBrowserProvider()) as rt:
        result = await rt.run(summarise_a_page, url)
        return str(result.output)
```

`LocalBrowserProvider` launches Chromium in this process. It cannot reattach —
a locally-launched browser dies with the process — so it refuses
`SessionScope.DURABLE` outright. `LocalBrowserProvider(engine="patchright")`
swaps in the Apache-2.0 anti-detection fork (`pip install 'loomsdk[stealth]'`);
LOOM ships no evasion of its own and treats bot detection as the provider's
problem.

Policy is per session:

```python
POLICY = BrowserPolicy(
    headless=True,
    scope=SessionScope.STEP,
    viewport=(1280, 900),
    max_wall_seconds=120.0,
)
```

`max_wall_seconds` matters more than it looks: without it a hung page holds a
provider slot until the run's lease expires, which is a much later and much less
obvious failure.

## Writing your own provider

Implement `BrowserProvider` and `BrowserSession`, then prove it:

```python
async def verify_my_adapter(provider, url: str) -> None:
    from loom.testing.conformance import verify_browser_session

    await verify_browser_session(
        provider,
        url=url,
        known=Target(role="button", name="Submit"),
        repeated=Target(role="button", name="Notify"),
    )
```

Point it at a fixture you control, never a third party's site, so a red test
means your adapter broke rather than someone else's server did.

The kit checks the things an author would not think to test — and one of them
above all: **a provider claiming `reattach` must return the *same* session.**
Quietly returning a fresh one looks like success while losing everything the run
had done, and every happy-path test still passes.

## What this does not do

- **Bot detection and CAPTCHAs are the provider's.** `supports()` declares
  `captcha` and `stealth` as capabilities; LOOM implements neither.
- **A frozen page cannot be tested for dynamic behaviour** — a date picker that
  fetches slots, a field that appears once another is filled.
- **There is no dry run.** The last step of learning a booking form is the
  booking. Nothing here makes a terminal side effect rehearsable, which is why
  the approval and the effect declaration exist.

## See also

- `examples/cookbook/30_browser_automation.py` — the basics, runnable offline
- `examples/cookbook/31_browser_approval.py` — the approval flow above, runnable
- `docs/seams/browser-provider.md`, `docs/seams/browser-session.md`
- `phases/phase-13-browser-automation.md` — why it is shaped this way
