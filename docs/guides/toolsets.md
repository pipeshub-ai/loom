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

### The three dialects

Whatever the API does, express it as "rows, and where the next page starts":

| Style | `cursor` is | Ends when |
|---|---|---|
| Token (`nextPageToken`) | the token | the token is absent, or `isLast` |
| Cursor (`_links.next`) | the parameter from that URL, not the URL | no next link |
| Offset (`startAt`/`total`) | the next offset as a string | offset ≥ total |
| Bare array (no envelope) | the next offset | a short page |

A bare array cannot always answer "was that everything?" — a full last page and
a truncated one look identical. `collect` reports `complete=False` rather than
claiming a completeness it cannot verify.

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
read-only toolset, and what a `GuardedBroker` weighs per call. `from_steps`
guesses from the operation name; pass `effects={"scrape": EffectClass.WRITE}`
where the name lies.

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
