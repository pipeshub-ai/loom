# Pagination: one view for the agent, one seam for toolset authors

<!-- docs-illustrative -->

**Goal.** A coding agent — including a small one — can see which operations
page, and copy a pattern that is correct. A developer adding the 1000th toolset
declares paging once, in the place they were already writing code, and gets the
same agent-facing view as the first four.

---

## 1. The audit, first

All 44 operations across `jira`, `confluence`, `gmail`, and `google_calendar`
were enumerated and compared three ways: what the tool returns, what the
manifest declares, and whether the client actually runs a paging loop.

| Toolset | Ops | Paged | Client loops |
|---|---|---|---|
| jira | 16 | 3 | `search_issues`, `get_comments`, `search_users` |
| confluence | 11 | 3 | `search_pages`, `get_page_comments`, `list_spaces` |
| gmail | 9 | 1 | `list_message_ids` |
| google_calendar | 8 | 1 | `list_events`, **`list_calendars`** |

Return type and manifest now agree everywhere. Two findings survive, and they
are what this plan is shaped around:

**F1 — `calendar_list_calendars` pages and throws the evidence away.** The
client calls `paginate(limit=250)`, then rebuilds a plain list from the result.
Manifest and return type agree that it does not page; both are wrong, and the
existing contract test cannot see it because it compares those two to each
other. **The client is the ground truth and nothing checks against it.**

**F2 — `jira_list_projects` uses the deprecated non-paging `GET /project`.**
Not truncating today; on an endpoint Atlassian has replaced with a paging one.

---

## 2. Limitations to solve

| # | Limitation | Evidence |
|---|---|---|
| **L1** | Coverage dies at the journal boundary — `encode(Results)` → `list` | Verified; produced the `max_results=100` / bare-count generation |
| **L2** | The contract test compares manifest to return type, not to the client | F1 |
| **L3** | Each client hand-writes its own `fetch` closure — six near-identical loops in three dialects | 3 toolsets, 3 dialects |
| **L4** | No pattern for an unbounded set; raising `max_results` is the only advice | mailbox, audit log |
| **L5** | The rule carries a caveat ("only inside the step"), so a small model will violate it | the observed generation |

---

## 3. Design

Four changes, each with one reason to exist.

<!-- docs-preamble -->

The sketches below resolve against these names — a proposal that does not even
import is a proposal nobody has checked.

```python
from typing import Any, Protocol, Self, runtime_checkable

from loom import Context, step, workflow
from loom.toolsets.pagination import Page, Results
from loom.toolsets.jira.models import JiraIssue as Issue


def _flatten_issue(raw: dict[str, Any]) -> Issue:
    return Issue(key=raw.get("key", ""), summary=raw.get("summary", ""))


class TokenPaging:
    def __init__(self, **_: Any) -> None: ...


class CursorPaging:
    def __init__(self, **_: Any) -> None: ...


class OffsetPaging:
    def __init__(self, **_: Any) -> None: ...
```

### 3.1 `Results` owns its serialisation (solves L1, L5)

The trap is that a step returning `Results` journals a `list`. Fix it where the
knowledge lives: `Results` declares how it round-trips, and `serde` gains a
general hook for "types that describe their own wire form" rather than a
special case for this one.

```python
# core/serde.py — one hook, not one branch per type
@runtime_checkable
class SelfEncoding(Protocol):
    def __wire__(self) -> dict[str, Any]: ...
    @classmethod
    def __from_wire__(cls, payload: dict[str, Any]) -> Self: ...
```

`Results.__wire__` emits `{items, complete, total, cursor}` under a reserved
key. Open/closed: a future type that needs the same treatment implements the
protocol and touches no serde code.

**The distinction that must survive:** a value *restored from its own envelope*
keeps `complete`; a value *validated from arbitrary data* does not, and defaults
to `complete=False`. The pydantic path already does this. Claiming completeness
that was never measured is worse than admitting ignorance.

Once this lands the rule loses its caveat, which is what makes it followable:

```python
@step
async def fetch_issues(jql: str) -> Results[Issue]:
    """Whatever the toolset returns for a paged read."""
    return Results([], complete=True)


@workflow(name="paged_report")
async def report(ctx: Context, jql: str) -> str:
    issues = await ctx.step(fetch_issues, jql)
    if not issues.complete:      # still true after the step boundary
        return f"showing {issues.summary()}"
    return f"{len(issues)} issues"
```

### 3.2 Paging dialects become named adapters (solves L3)

Today every client writes its own `fetch` closure. Three dialects exist and
each is re-derived per operation — six closures, and the 1000th toolset writes
a seventh.

```python
# toolsets/pagination.py
class PagingStyle(Protocol):
    """Turns one raw response into rows plus where the next page starts."""
    def read(self, response: Any) -> Page: ...
    def apply(self, params: dict, cursor: str | None, size: int) -> dict: ...

TokenPaging(items="issues", token="nextPageToken", last="isLast")
CursorPaging(items="results", link=("_links", "next"), param="cursor")
OffsetPaging(items="comments", start="startAt", total="total")
```

A toolset author writes:

```python
class JiraClientSketch:
    async def search_issues(
        self, jql: str, max_results: int = 20
    ) -> Results[Issue]:
        return await self.paged(
            "search/jql",
            style=TokenPaging(items="issues", token="nextPageToken", last="isLast"),
            limit=max_results,
            row=_flatten_issue,
            body={"jql": jql},
        )

    async def paged(self, *args: Any, **kwargs: Any) -> Results[Issue]:
        """Supplied by the shared client base — see 3.2."""
        return Results([])
```

Single responsibility: the style knows a wire format and nothing about Jira;
the client knows Jira and nothing about looping; `collect` knows looping and
nothing about either. A new dialect is a new `PagingStyle`, not an edit to
`collect`.

**Bare-array endpoints** (Jira `user/search`) get `OffsetPaging(items=None)` —
a short page is the only end signal, and `complete` stays `False` when the last
page is full, which is already tested.

### 3.3 The contract test becomes three-way (solves L2)

Client behaviour is the ground truth:

> **A client method that runs a paging loop ⟹ its tool returns `Results[T]` ⟹
> its manifest declares `pagination=True`.**

Checked by walking client sources for `collect(`/`paged(` and mapping method to
tool. F1 is the proof this is needed: manifest and return type agreed with each
other and both disagreed with the code.

### 3.4 The agent's view: one card, one snippet (solves L4, L5)

`describe()` already names the paged reads. Two additions, both *generated from
the manifest* so they name real functions and cost nothing to maintain:

**A complete, copyable snippet** — a small model needs to copy, not infer:

```text
Paged reads: jira_search_issues, jira_get_comments, jira_search_users
  Bounded — one call, and say what it covers:
      found = await ctx.step(jira_search_issues, jql, max_results=200)
      if not found.complete:
          header = f"showing {found.summary()}"
  Unbounded (a mailbox, a log) — one step per page, resumable:
      cursor = await ctx.state.get("cursor")
      page = await ctx.step(jira_search_issues, jql, cursor=cursor)
      await ctx.state.set("cursor", page.cursor)
```

The second form is why "raise `max_results`" is not the answer: one call
fetching 50,000 rows is one journal entry, so a crash refetches all of them and
the whole page sits in memory. One step per page is journaled per page and
resumes where it stopped.

**A `cursor` parameter on paged operations**, so the second form is expressible
at all. Optional, defaulting to `None`, so every existing call is unchanged.

---

## 4. Phases

Each leaves `main` releasable and green.

| Phase | Work | Exit |
|---|---|---|
| **P1** | `SelfEncoding` + `Results` round-trip | A `Results` returned from a step keeps `complete` after replay; a validated one does not claim it |
| **P2** | Three-way contract test; fix F1, F2 | The test fails on today's `calendar_list_calendars`, passes after |
| **P3** | `PagingStyle` adapters; migrate all six loops | Six closures become three styles; per-toolset paging code drops to one call |
| **P4** | `cursor=` parameter, docs card, snippet generation | `describe()` shows both patterns naming real functions |
| **P5** | Prompt: replace the caveat with the two named patterns | `CoverageStage` still fires on the original bad generation |

P1 before P4: the snippet is only honest once the flag survives the boundary.

---

## 5. Tests

| Level | What |
|---|---|
| Unit | each `PagingStyle` against its dialect, including a bare array and a full last page |
| Round-trip | `Results` through `encode`/`decode`, through a journal, and through a replay |
| Contract | client ⟹ return type ⟹ manifest, over **every** toolset, driven by the registry so a new one is covered without editing the test |
| Regression | the observed generation still trips `CoverageStage`; the corrected form does not |
| Extensibility | a fake third-party toolset defined in the test declares paging and appears in `describe()` — proving the developer story without shipping a toolset for it |
| Docs | the generated snippet is executed, not just asserted on |

The extensibility test is the one that matters for the 1000-toolset claim: it
proves a developer outside this repo gets the same agent-facing view.

---

## 6. Risks

**A `Results` envelope in the journal is a payload change.** Runs journaled
before it must still replay. Mitigation: decode accepts both shapes; a bare
list decodes to a plain list exactly as today.

**Detecting client paging by source inspection is fragile.** It is a test-only
heuristic and fails *closed* — an unrecognised loop reports a missing
declaration rather than passing silently.

**`cursor=` widens every paged signature.** Kept optional, absent from the
index card, and mentioned only in the unbounded snippet, so the common case
does not pay for it.
