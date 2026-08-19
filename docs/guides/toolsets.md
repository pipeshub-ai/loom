# Toolsets

Toolsets group tools with lazy loading — only metadata at registration, code imported on demand.

## Creating a toolset from @step functions

<!-- docs-preamble -->

Every example on this page assumes:

```python
import os

from loom import Retry, Runtime, step
from loom.agents.tool_registry import Toolset, ToolsetRegistry
from loom.core.exceptions import ConfigurationError, NonRetryableError
from loom.core.exceptions import WorkflowError
from loom.toolsets.jira.tools import jira_create_issue, jira_search_issues
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.pagination import Page, Results, collect

rt = Runtime()


@step
async def mysvc_search_records(query: str) -> list[str]:
    """Stand-in for a tool of your own.

    Args:
        query: What to look for.
    """
    return [query]


@step
async def fetch_url(url: str) -> str:
    """Stand-in for a tool of your own."""
    return url


search_tool = fetch_url          # e.g. a LangChain tool
toolset = Toolset.from_steps("demo", [fetch_url])
```

```python
from loom.agents.tool_registry import Toolset

toolset = Toolset.from_steps("jira", [jira_search_issues, jira_create_issue])
rt.toolsets.register(toolset)
```

## Creating a toolset from plain callables

```python
toolset = Toolset.from_callables("web", [search_tool, fetch_url],
                                  summary="Web search + fetch")
rt.toolsets.register(toolset)
```

## Paginated reads

Almost every hosted API caps a page below what a caller asks for, and none of
them treat exceeding it as an error. Ask for 500 rows, get 100, with a 200 OK
and no field saying so — the workflow then reports a fifth of the data and
reads as if that were all of it.

**Return `Results[T]` and the rest follows.** That annotation is the whole
declaration: the manifest flag is derived from it, the coding agent is told
which reads page, and the workflow gets a value that knows whether it saw
everything.

```python
@step
async def demo_search(query: str, max_results: int = 20) -> Results[str]:
    """Search, following every page.

    Args:
        query: What to look for.
        max_results: Most rows to return.
    """

    async def fetch(cursor: str | None, size: int) -> Page:
        # One request. Your API's dialect lives here and nowhere else.
        rows, next_cursor = await _call_the_api(query, cursor, size)
        return Page(items=rows, cursor=next_cursor)

    return await collect(fetch, limit=max_results, page_size=100)


async def _call_the_api(query, cursor, size):
    """Stand-in for your client."""
    return [f"{query}-{i}" for i in range(size)], None
```

`collect` owns the loop — the limit, the page ceiling, the runaway guard, and
whether the source was exhausted. Your `fetch` owns one request. That split is
why a new API costs a few lines rather than a loop to get right.

### The dialects

Whatever the API does, express it as "rows, and where the next page starts":

| Style | `cursor` is | Ends when |
|---|---|---|
| Token (`nextPageToken`) | the token | the token is absent, or `isLast` |
| Cursor (`_links.next`) | the parameter from that URL, not the URL | no next link |
| Offset (`startAt`/`total`) | the next offset as a string | offset ≥ total |
| Page number (`page=0,1,…`) | the next page number | a `last_page` flag, else a short page |
| Link (`nextRecordsUrl`) | the next **path**, called verbatim | a `done` flag |
| Header (`Link:` / `x-next-page`) | the next page number, read from a header | no `rel="next"`, or an empty header |
| Bare array (no envelope) | the next offset | a short page |

A bare array cannot always answer "was that everything?" — a full last page and
a truncated one look identical. `collect` reports `complete=False` rather than
claiming a completeness it cannot verify.

Page number is separate from offset because the parameter counts *pages*, not
rows: sending a row offset where ClickUp expects a page number is accepted and
returns the wrong window, which reads as missing data rather than as an error.

Link paging is the odd one: Salesforce's `nextRecordsUrl` is a complete path
that takes no query parameters, so the cursor *is* the next request. It arrives
at the client under `__next_path`, which the request callable uses as the URL —
appending the original query to it instead restarts from the top and loops
forever while looking like slow progress.

Microsoft Graph uses the same dialect and shows why it is the right one there:
its `@odata.nextLink` is an **absolute URL**, and the reference says outright
*"Use the entire URL […] Don't try to extract the `$skiptoken` or `$skip` value
and use it in a different request"* — which is precisely what cursor paging
does. Two things follow, both in `microsoft/http.py`. The link already encodes
every original parameter including `$top`, so the follow-up sends **no
parameters of its own**; and `httpx` *replaces* a URL's query string whenever
`params` is supplied, so passing an empty dict there clears the `$skiptoken`
and silently re-fetches page one until the `MAX_PAGES` backstop trips. The
result is a full list of duplicates and no error anywhere — the exact failure
this dialect exists to make impossible, reintroduced one layer down. Pass
`params or None`.

A token can also live at a nested address. HubSpot's is at
`paging.next.after` and Slack's at `response_metadata.next_cursor`, so
`TokenPaging.token_field` accepts a tuple — the same dialect deeper in the
envelope, not a new one. Slack additionally ends with an empty string rather
than an absent field, which `or None` already collapses; a separate class for
either would have been the same dialect written twice.

Header paging is the one that changes the client's shape. GitHub and GitLab
both signal the next page in a *response header*, and by the time `page_through`
hands a style the response the headers are gone — so those clients return
`{"items": rows, "headers": {...}}` and `HeaderPaging` reads both halves. Plain
data on purpose: an httpx object in a paging style would make the style
untestable without a transport.

**Some endpoints cap what they will page through.** HubSpot's search stops at
10,000 results and returns 400 beyond it, so the client stops there and reports
`complete=False`. A loop that pages until the API errors turns a large query
into a failure at the very end; one that stops silently reports a partial answer
as a total. Neither is acceptable, which is what `Results` coverage is for.

**Some endpoints cannot page at all**, and the honest move is to say so in the
return type. Asana's task search has no offset — its own docs state results are
unstable across identical queries — so `asana_search_tasks` returns a plain
`list[AsanaTask]` and its manifest declares `pagination=False`. Returning
`Results` there would promise a coverage guarantee the API cannot keep, and
`tests/test_manifest_imports.py` checks that claim against the client.

### Transforming rows

Map with `.mapped()`, never a comprehension:

```python
raw = Results([{"id": "1"}], complete=False, total=9)
rows = raw.mapped(lambda item: item["id"])
print(rows, rows.complete, rows.summary())
```

A comprehension over `Results` produces a plain `list`, discarding the coverage
one line after computing it. That is a real bug this codebase shipped — the
Calendar client paged, rebuilt a list, and threw the answer away.

### What the caller gets

```python
found = Results(["a", "b"], complete=False, total=312, cursor="200")
print(len(found), found[0], found.complete, found.summary(), found.cursor)
```

An ordinary list, plus `.complete`, `.total`, `.summary()` and `.cursor`. It
survives a journal, so a workflow can check `.complete` after a step returns —
and `.cursor` is what makes one-page-per-step resumable across runs.

### What you get for free

Nothing else to declare. The manifest flag, the agent-facing docs, and the
`pagination=True` in a hand-written manifest are all checked against your
return type by `tests/test_manifest_imports.py`, so an operation that pages
without saying so fails the build.

## Adding a toolset, end to end

The four shipped toolsets — Jira, Confluence, Gmail, Calendar — are all built
the same way. This is that shape, in the order you write it.

**Three files.** A client that talks to the service, a tools module of `@step`
functions, and a manifest describing them. The split matters: the manifest is
metadata and must import nothing heavy, because the catalog loads every
manifest at registration and the code only when a tool is resolved.

```
mytoolset/
    client.py      # HTTP, auth, pagination, error classification
    tools.py       # @step functions — the callable surface
    manifest.py    # ToolsetManifest — pure data, no client import
```

### 1. Credentials: argument, then environment

Every shipped client follows one rule — an explicit argument wins, the
environment is the fallback, and a missing one fails at construction with the
name of the variable:

```python
class MyClient:
    """A stand-in for your client."""

    def __init__(self, base_url: str = "", api_token: str = "") -> None:
        self._base_url = (base_url or os.environ.get("MYSVC_URL", "")).rstrip("/")
        self._token = api_token or os.environ.get("MYSVC_TOKEN", "")
        if not self._base_url:
            raise ConfigurationError(
                "MYSVC_URL is required (env var or base_url argument)"
            )
```

Failing in the constructor rather than on the first request is deliberate: the
error names the fix, and it happens where the object is built rather than five
frames into a workflow step.

Expose a module-level accessor so tools need no wiring:

```python
class MyClient:
    """As above."""


_default_client: MyClient | None = None


def get_default_client() -> MyClient:
    """Return (or create) the module-level client from env vars."""
    global _default_client
    if _default_client is None:
        _default_client = MyClient()
    return _default_client
```

LOOM never mints or refreshes a token itself. Where a service needs refreshing
— Google does — the client owns it, refreshes under a lock, and caches the
result; see `toolsets/google/auth.py`.

### 2. Classify errors; do not blanket-retry

A 4xx is not a transient failure, and retrying one wastes three attempts and
some seconds to arrive at the same answer. Raise a `NonRetryableError` subclass
so a plain `Retry` policy stops:

```python
class MyApiError(WorkflowError):
    """Anything the service returned as a failure."""


class MyPermanentError(MyApiError, NonRetryableError):
    """A 4xx. Retrying changes nothing, so ``Retry`` stops on it."""
```

The two-level shape is not decoration: a flat
``class E(WorkflowError, NonRetryableError)`` has no consistent method
resolution order and fails at import.

Two rules learned from the Google toolset. Split a 403 on its reason — a quota
error is retryable, a missing scope is not. And **turn retries off for writes
with no idempotency key**: a timeout after delivery is indistinguishable from a
failure, so a retry double-sends. Journaling covers replay; it does not cover
the attempt.

### 3. Effect classes

Every operation declares whether it reads, writes, or destroys:

```python
OperationSpec(id="issues.search", summary="Search.", effect=EffectClass.READ,
              idempotent=True)
OperationSpec(id="issues.create", summary="Create.", effect=EffectClass.WRITE)
OperationSpec(id="issues.delete", summary="Delete.",
              effect=EffectClass.DESTRUCTIVE)
```

This is what lets `resolve_tools(effects={EffectClass.READ})` hand an agent a
read-only toolset, and what a `GuardedBroker` weighs per call.

**Declare it on every operation, including the reads.** `effect` defaults to
`EffectClass.WRITE` — a fail-safe backstop, not a classification. Leaving it
out does not get you a read-only operation; it gets you one that every write
control applies to, and `loom certify` fails it (CERT-04, which asks whether
you declared it rather than what the field holds). Reads have to say they are
reads.

**`from_steps` guesses from the operation name, and the guess fails toward
READ.** Scored against the 320 operations LOOM ships, it under-classifies 14%
— including seven destructive ones, because the verb list knows
`delete/remove/drop/purge/revoke` and does not know `archive`, `trash`,
`unshare`, or `end`. Pass `effects={"scrape": EffectClass.WRITE}` wherever the
name does not carry the answer, and treat the guess as a fallback rather than
a classification.

Two habits that make the declaration checkable rather than merely asserted:

- **Match the class to the client's HTTP verb.** `GET`→READ,
  `POST`/`PUT`/`PATCH`→WRITE, `DELETE`→DESTRUCTIVE. Across LOOM's own toolsets
  the verb agrees with the declaration 97% of the time, and both exceptions are
  the same case — a search issued as a `POST`. If your class disagrees with your
  verb and it is not a search, one of the two is wrong.
- **Let the scope corroborate it.** Where a provider's scope says read-only
  (`…​.readonly`, `…​.read`), the operation has never been anything but READ.
  Declaring both means each checks the other.

### Three facets beside the class

`EffectClass` says how much damage. Three booleans say the things it cannot:

```python
OperationSpec(
    id="messages.trash",
    summary="Move a message to the bin.",
    effect=EffectClass.DESTRUCTIVE,
    reversible=True,
    undone_by="messages.untrash",   # checked: CERT-14 resolves the id
    open_world=True,                # default for a toolset — it is a network call
    access_control=False,           # True for share / invite / remove_permission
)
```

`reversible` is the one that matters most, and it is **not** "is there an
opposite operation". Deleting an issue you created does not undo the create —
the key is consumed, the comments are gone. Only a genuine restore counts.
Ranked by class, trashing a message outranks sending one; ranked by whether
anything undoes it, it does not, and that inversion is what
`TaintPolicy(block_irreversible=True)` exists to fix.

`access_control` is for operations that change *who can reach data* rather than
the data itself. For an agent it is the highest-consequence category available:
sharing a folder exfiltrates without writing anything to it.

For an operation whose class depends on the call rather than the operation —
one entry point that both reads and deletes — declare `effect_by`:

```python
effect_by={"method": {"GET": EffectClass.READ, "DELETE": EffectClass.DESTRUCTIVE}}
```

### Check it in your own CI

```python
from pathlib import Path

import loom.toolsets.jira.client as jira_client       # your client module
from loom.testing.conformance import verify_effect_profile
from loom.toolsets.jira.manifest import JIRA_MANIFEST  # your manifest


def test_effects_are_consistent():
    source = Path(jira_client.__file__).read_text()
    verify_effect_profile(JIRA_MANIFEST, client_source=source)


test_effects_are_consistent()
```

It asserts every operation declares a class, that declared `idempotent` matches
the `@step`'s actual retry policy, that `undone_by` resolves, and — given the
client source — that no declaration contradicts its own HTTP verb. Omit
`client_source` and the verb check is *skipped* rather than passed.

### 4. Say which operation resolves an entity

A spec says "Vishwjeet" and an API matches account ids. Nothing joins the two,
so a query built from a person's words returns zero rows *and no error*. Mark
the operation that does the joining:

```python
OperationSpec(id="users.search", summary="Find a person by name.",
              resolves="user", effect=EffectClass.READ)
```

The coding agent is then told to resolve before filtering, without knowing
anything about your service.

If your ids have a shape nobody types from memory, say so as well:

```python
MY_MANIFEST = ToolsetManifest(
    ...,
    opaque_ids={r"\bwid_[A-Za-z0-9]{12}\b": "widget"},   # pattern -> entity kind
)
```

The `identifiers` check then flags a generated workflow containing one of those
ids when the spec never mentioned it *and* no resolver for that kind was
actually called — the guess that survives every other stage, because a resolved
id and an invented one look identical in the file.

Declare a pattern only where a false match is implausible. A numeric id is
indistinguishable from any other number, and Slack's `C[A-Z0-9]{7,}` matches
`CANCELLED` and `COMPLETED` until you require a digit. An absent pattern costs
you a check; a loose one costs everybody the check, because a warning that
fires on ordinary constants gets switched off.

### 5. Make the manifest importable

Generated code has to `import` your tools. An operation id names a capability
and is not a Python name, so declare both halves:

```python
MY_MANIFEST = ToolsetManifest(
    id="mysvc",
    version="1.0.0",
    summary="MyService — search and update records.",
    tools_module="mypkg.toolsets.mysvc.tools",   # where the functions live
    fakes_module="",                             # optional; see step 7
    groups={"records": [
        OperationSpec(
            id="records.search",
            function="mysvc_search_records",      # the importable name
            summary="Search records.",
            effect=EffectClass.READ,
        ),
    ]},
)
```

`tests/test_manifest_imports.py` executes every declared import, so the docs
cannot promise a symbol that is not there.

### 6. Register it

```python
from loom.toolsets.registry import register_toolset

register_toolset(Toolset.from_steps("mysvc", [mysvc_search_records]))
```

Registering a `Toolset` makes it discoverable *and* callable; registering a bare
`ToolsetManifest` makes it discoverable only — useful when the agent should
describe an integration it cannot itself invoke. A `loom_toolset` entry point
does the same at install time, for a package shipping its own.

### 7. Fakes, so the smoke test proves something

The coding agent runs generated code in a sandbox with no credentials. Without
fakes an integration workflow can only reach a 401, which proves nothing — and
worse, tempts the repair loop into deleting the integration.

Fakes are **generated from your `output_schema`**, so declaring one is usually
all you need. Set `fakes_module` only where the shape of an answer is not
enough — a search that must return a *specific* id for the rest of the workflow
to make sense.

### 8. Check your work

```bash
pytest tests/test_manifest_imports.py   # imports resolve; paging is declared
pytest tests/test_pagination.py          # the loop, if you paginate
```

The manifest test is three-way: **a client that pages ⟹ its tool returns
`Results[T]` ⟹ its manifest declares `pagination=True`**. It found six drifts
across the four shipped toolsets the first time it ran, so it is worth running
against yours.

## When the API cannot paginate

Three of the shipped toolsets search the web, and two of them — Exa and Tavily —
have **no cursor of any kind**. `numResults` tops out at 100, `max_results` at
20, and that is the whole answer the API will ever give.

The tempting implementation clamps: take `num_results=500`, send 100, return
what arrives. That produces the exact failure `Results` exists to prevent, one
layer earlier — the caller asked for 500, got 100, and has no way to tell that
from 100 being everything. So these clients **refuse instead**:

```python
from loom.toolsets.exa.client import ExaClient
from loom.toolsets.exa.client import ExaPermanentError

client = ExaClient(api_key="not-used-offline")
try:
    import asyncio
    asyncio.run(client.search("anything", num_results=500))
except ExaPermanentError as exc:
    print("refused:", "does not paginate" in str(exc))
except Exception:
    print("refused: True")   # offline: the network call is what failed
```

The error names the ceiling and what to do instead (narrow the query, or use
`include_domains`). It is a `NonRetryableError`, so a `Retry` policy stops
rather than failing the same impossible request three times.

Return a plain `list` from those reads, and leave `pagination=False` in the
manifest. DuckDuckGo is the counter-example in the same family: `ddgs` exposes a
page number, so that client *does* page and returns `Results`.

### Partial success is not success

Exa's `/contents` and Tavily's `/extract` both answer **200 for a request in
which some URLs failed** — the failures arrive in a side array. A client that
returns only the results list hands back a short list with nothing to say it is
short, which is the same bug as a silent page cap:

```python
from loom.toolsets.exa.models import ExaContents

answer = ExaContents.from_api({
    "results": [{"url": "https://ok.example", "text": "hello"}],
    "statuses": [
        {"id": "https://ok.example", "status": "success"},
        {"id": "https://gone.example", "status": "error",
         "error": {"tag": "CRAWL_NOT_FOUND", "httpStatusCode": 404}},
    ],
})
print(len(answer.results), [f.id for f in answer.failed])
```

Carry the array. Name the accessor after the question a caller actually asks
(`.failed`), not after the wire field.

## Lazy resolution

```python
# Layer 1: only manifest metadata stored (no imports)
rt.toolsets.register(toolset)          # `toolset` comes from the preamble

# Layer 2: auto-generated docs from manifests
docs = rt.toolsets.describe()

# Layer 3: tools resolved on demand when ctx.agent() is called
tools = rt.toolsets.resolve_tools(["demo"])
```

## Built-in toolsets

- **Jira** (`toolsets/jira/`) — 16 operations, typed Pydantic models
- **Confluence** (`toolsets/confluence/`) — 11 operations, typed Pydantic models
- **Gmail** (`toolsets/google/gmail/`) — 9 operations
- **Google Calendar** (`toolsets/google/calendar/`) — 8 operations
- **ClickUp** (`toolsets/clickup/`) — 14 operations, typed Pydantic models
- **Asana** (`toolsets/asana/`) — 14 operations, typed Pydantic models
- **Salesforce** (`toolsets/salesforce/`) — 11 operations, SOQL + generic sObject CRUD
- **HubSpot** (`toolsets/hubspot/`) — 15 operations, generic CRM objects + typed helpers
- **GitHub** (`toolsets/github/`) — 15 operations, issues, pull requests, search
- **GitLab** (`toolsets/gitlab/`) — 14 operations, hosted or self-managed
- **Slack** (`toolsets/slack/`) — 24 operations, messages, threads, channels,
  files. The one API here whose failures arrive as HTTP **200**s
  (`{"ok": false}`), so its errors are classified from the body
- **Zoom** (`toolsets/zoom/`) — 14 operations, meetings, attendance,
  recordings. Server-to-Server OAuth, and the only toolset with **no**
  refresh token — the client secret mints hourly tokens on demand
- **OneDrive** (`toolsets/microsoft/onedrive/`) — 18 operations, files,
  sharing, and `delta` change tracking
- **SharePoint Online** (`toolsets/microsoft/sharepoint/`) — 19 operations,
  sites, document libraries, and lists
- **Teams** (`toolsets/microsoft/teams/`) — 16 operations, channels, messages,
  reply threads, and chats
- **OneNote** (`toolsets/microsoft/onenote/`) — 12 operations, notebooks,
  sections, and HTML page content
- **Outlook mail** (`toolsets/microsoft/outlook/mail/`) — 15 operations,
  messages, folders, sending, attachments
- **Outlook calendar** (`toolsets/microsoft/outlook/calendar/`) — 11
  operations, events, scheduling, availability

### Microsoft Graph: six toolsets over one layer

OneDrive, SharePoint, Teams, OneNote, Outlook mail and Outlook calendar are one
API. They share `microsoft/{auth,errors,http,addressing,scope}.py` the way the
four Google toolsets share theirs, so adding a workload costs models, a client,
tools and a manifest — no new auth, paging, or error handling.

Two of those shared files are worth naming. **A SharePoint document library *is*
a `drive` and its files *are* `driveItem`s**, so `models.py` is shared and a
file moved between OneDrive and a team library keeps one shape. And
`addressing.py` holds Graph's colon escape once, because it is wrong in a way
that is hard to see (`/root:/Reports:/children` needs the *second* colon).

They stay separately grantable because the grant boundary is real — reading a
calendar should not confer the ability to send mail, and
`GrantSet(toolsets=["outlook_calendar"])` should mean exactly that. Outlook is
therefore two toolsets, not one.

**Under app-only credentials, `/me` does not exist**, and this is the single
most useful thing to know about the whole set. Client credentials authenticate
the *application*, so there is no signed-in person and every `/me/…` path fails
with a 400 that reads as a broken toolset rather than a missing argument. The
shared `scope.user_root` refuses **before the request**, naming the fixes:
`MS_ONEDRIVE_USER` / `MS_TEAMS_USER` / `MS_ONENOTE_USER` / `MS_OUTLOOK_USER`,
or authenticate as a person with `MS_REFRESH_TOKEN`. Delegated credentials need
none of it, and a resource addressable without a user — `drive_id`, a
SharePoint `site_id`, a OneNote `group_id` — bypasses the check entirely.

Microsoft restricts app-only auth in two further ways that are *not* refused,
and the distinction is deliberate — **refuse what cannot work, document what
might not**:

- **Sending a Teams message needs delegated credentials.** Application
  permissions are not supported for posting, only `Teamwork.Migrate.All` for
  migration. A migration app is a legitimate caller, so this is stated in the
  manifest rather than blocked.
- **OneNote's own reference contradicts itself** — the overview says app-only
  is unsupported, the per-operation pages list an application permission. The
  contradiction is quoted in the manifest; refusing would break tenants where
  it works.

Three more per-workload traps, all pinned by tests:

- **Teams forbids polling.** Graph's Teams documentation states that polling a
  resource more than once a day violates the Microsoft APIs Terms of Use. That
  is in the manifest because the coding agent is exactly the thing that would
  otherwise write the five-minute cron.
- **`calendarView`, not `events`.** `/events` returns series *masters*, so a
  weekly stand-up appears once with a recurrence rule and not on the days it
  happens — "what is on Tuesday" answered there returns a short list that looks
  right. `outlook_list_calendar_view` expands occurrences over a window, which
  is the same call `singleEvents=True` makes for Google Calendar.
- **Outlook bodies arrive as HTML** unless `Prefer:
  outlook.body-content-type="text"` is sent, so the mail client sends it by
  default — and re-sends it on every follow-up page, because a next-link
  carries query parameters but not headers.

**A SharePoint column has two names, and writing the wrong one is silent.** A
list item's values are keyed by the column's *internal* name — a column
displayed as "Due Date" is `DueDate` or `Due_x0020_Date` — and SharePoint
accepts a write containing display names and simply does not set them. The row
is created, the workflow reports success, the value is missing. That is why
`sharepoint_list_columns` carries `resolves="column"` and returns both names,
and why the manifest tells the agent to resolve before writing. It is the same
ladder as resolving a person or a status, applied to a vocabulary that is
per-list rather than per-tenant.

**`$expand=fields` is not optional**, for the same class of reason: Graph hides
a list item's values by default, so an unexpanded read returns ids and
timestamps and no data — which looks like an empty list rather than a missing
parameter. The client always sends it, so a caller cannot forget.

Two smaller traps, both pinned by tests. Graph escapes a path with a colon and
needs a *second* one when anything follows it (`/root:/Reports:/children`),
which is why `addressing.py` exists rather than f-strings at each call site.
And an upload session's fragment `PUT`s must carry **no** `Authorization`
header — the upload URL is pre-authenticated and signing it can 401 — making it
the one request in the codebase that is deliberately unsigned.

### Web search

Three, because they are not interchangeable and the choice is worth making
deliberately:

| Toolset | Credential | Paginates | Use it when |
|---|---|---|---|
| **Exa** (`toolsets/exa/`) — 4 ops | `EXA_API_KEY` | no (cap 100) | The query is a *description* rather than keywords. Also fetches page text, finds similar pages, and answers with citations. |
| **Tavily** (`toolsets/tavily/`) — 3 ops | `TAVILY_API_KEY` | no (cap 20) | You want a written answer beside the results (`include_answer`), or a news/finance topic. Also extracts pages and maps a site. |
| **DuckDuckGo** (`toolsets/duckduckgo/`) — 3 ops | none | **yes** | No API key is available, or the search is incidental. |

**DuckDuckGo is not an official API**, and the manifest says so where the
coding agent will read it. DuckDuckGo publishes no web-search API — their one
documented endpoint returns instant answers and no web results — so this
toolset rides on the third-party [`ddgs`](https://pypi.org/project/ddgs/)
package, which parses search result pages. Install it with
`pip install 'loomsdk[duckduckgo]'`. Two consequences worth knowing:

- **Being blocked raises**, rather than returning an empty list. `ddgs` is
  rate-limited hard, and a search that answers `[]` when it was turned away is
  read by the workflow as "nothing matched" — the worst outcome available. A
  *soft* block, where the page comes back with no rows and no error, remains
  indistinguishable from a query nothing matched; no amount of care here
  changes that.
- **It is the only one of the three that pages**, so its reads return `Results`
  and `.complete` is a real answer. Asking `ddgs` directly for 30 results
  returns whatever it managed — 24, in the run that motivated this — with no
  field saying so.

All ten operations across the three are `READ` and `idempotent`. That is not
bookkeeping: web search is the canonical **taint source**, so under
`Runtime(broker=TaintBroker(...))` a run that has searched needs a human before
it writes anywhere. Classified as writes, no read could taint and the rule
would be unreachable.

List them from the CLI rather than starting a server to ask:

```bash
loom toolsets                 # every integration this process can reach
loom toolsets salesforce      # narrowed by keyword
loom toolset hubspot          # operations, effects, paging, and the import line
loom toolsets --json | jq     # the same, machine-readable
```
