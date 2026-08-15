"""Collecting every page, and admitting when you did not.

Every hosted API caps a page well below what a caller may ask for — Jira at
100, Confluence at 250, Gmail at 500 — and none of them treat exceeding it as
an error. Ask for 500 issues and you get 100, with a 200 OK and no field saying
so. The workflow then reports on a fifth of the data and reads as if that were
all of it.

That is the same failure the entity-resolution work exists to prevent: **fewer
rows and no error**, which is indistinguishable from "there is nothing else".
It is worse here, because it only shows up once real data grows past one page —
so it passes every test written against a fixture and fails in production.

Two halves to the fix, and the second one matters more.

**Follow the pages.** `collect` runs the loop: request, take the batch, ask for
the next, stop at the caller's limit or when the source is exhausted.

**Say whether you got everything.** `collect` returns :class:`Results`, a list
that also knows whether the source ran out. A caller that does not care keeps
treating it as a list; a caller that does can check ``.complete`` and say "top
50 of 300" instead of implying it saw all 300.

The loop is generic. The *dialect* is not — token, cursor, and offset paging
all exist, and pretending otherwise produces an abstraction that fits none of
them. So the caller supplies one function that fetches a page and says where
the next one starts, and this module owns everything around it.

>>> import asyncio
>>> from workflow_builder.toolsets.pagination import Page, collect
>>> async def fetch(cursor, size):
...     start = int(cursor or 0)
...     rows = list(range(start, min(start + size, 7)))
...     return Page(rows, str(start + size) if start + size < 7 else None)
>>> found = asyncio.run(collect(fetch, limit=10, page_size=3))
>>> list(found), found.complete
([0, 1, 2, 3, 4, 5, 6], True)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from workflow_builder.core.serde import register_wire_type

__all__ = [
    "CursorPaging",
    "OffsetPaging",
    "Page",
    "PagingStyle",
    "Results",
    "TokenPaging",
    "collect",
    "page_through",
    "paginates",
]

T = TypeVar("T")

#: Stop after this many requests for one call, whatever the limit says. A
#: server that keeps handing back a cursor and no rows would otherwise loop
#: forever, and a workflow that hangs is harder to diagnose than one that
#: returns a short answer and says it is short.
MAX_PAGES = 50


@dataclass(frozen=True)
class Page:
    """One page, and where the next one starts."""

    items: list[Any]
    cursor: str | None = None
    """Opaque continuation — a token, a URL, or an offset as a string. ``None``
    means the source is exhausted."""
    total: int | None = None
    """How many exist in all, when the API says. Most do not."""


@register_wire_type
class Results(list[T], Generic[T]):
    """Everything collected, plus whether that is everything there is.

    **This type is the declaration.** A read that returns ``Results[Issue]`` is
    a paged read; one that returns ``list[Issue]`` is not. Nothing else has to
    be written down, which is the only version of this that survives a thousand
    toolsets — a ``pagination=True`` maintained by hand beside the code is a
    second source of truth, and second sources of truth drift.

    Generic over the item type, because the annotation is load-bearing twice
    over: it declares the paging *and* it is where the schema builders and the
    fake generator learn what a row looks like. A bare ``Results`` would say
    the first and silently lose the second.

    A ``list`` subclass rather than a wrapper, so every existing caller —
    ``len()``, iteration, indexing, ``for issue in issues`` — keeps working
    unchanged. The extra fact rides along for the callers that want it.

    One sharp edge, stated because it will otherwise be discovered the hard
    way: this degrades to a plain ``list`` when journaled, sliced, or
    serialised. ``complete`` describes *this fetch*, not the data, so a value
    replayed from a journal has no business claiming it. Read it at the call
    site, and put what it told you into the output.
    """

    def __init__(
        self,
        items: list[T] | None = None,
        *,
        complete: bool = True,
        total: int | None = None,
        cursor: str | None = None,
    ) -> None:
        super().__init__(items or [])
        self.complete = complete
        """False when the source had more and the limit cut it off."""
        self.total = total
        """How many matched in all, when the API says so."""
        self.cursor = cursor
        """Where the next page starts, when this one stopped short.

        Raising ``max_results`` is not an answer for a set with no natural
        bound — a mailbox, an audit log — and it is the wrong answer inside a
        durable workflow even when it fits: one call that fetches 50,000 rows is
        one journal entry, so a crash re-fetches all of them and the whole page
        has to be held in memory at once.

        Carrying the cursor makes the other shape possible: one step per page,
        each journaled, the cursor kept in ``ctx.state`` between runs. That
        resumes where it stopped instead of starting again.
        """

    def mapped(self, transform: Callable[[Any], Any]) -> Results[Any]:
        """Every row through *transform*, coverage carried over.

        The operation that was quietly losing it. A client pages raw JSON and
        then rebuilds it into models with a comprehension — and a comprehension
        over a ``Results`` produces a ``list``, so the answer to "did I see
        everything?" is discarded one line after being computed. That is the
        shape of the bug this method exists to make unavailable.
        """
        return type(self)(
            [transform(item) for item in self],
            complete=self.complete,
            total=self.total,
            cursor=self.cursor,
        )

    def __wire__(self) -> dict[str, Any]:
        """Everything, including whether this was everything.

        What makes the rule followable. Without it a step returning ``Results``
        journals a bare list, so ``.complete`` is readable inside the step and
        gone the moment it is returned — and a rule with a boundary caveat is a
        rule that gets violated, which is exactly how a workflow came to report
        one page as a total.
        """
        from workflow_builder.core.serde import encode

        return {
            "items": [encode(item) for item in self],
            "complete": self.complete,
            "total": self.total,
            "cursor": self.cursor,
        }

    @classmethod
    def __from_wire__(cls, payload: dict[str, Any]) -> Results[Any]:
        """Rebuild from :meth:`__wire__`, coverage intact.

        Distinct from the pydantic path, which validates arbitrary data and so
        defaults to ``complete=False``. Here the envelope *is* the record of a
        real fetch, so the answer it carries is the answer that was measured.
        """
        return cls(
            payload.get("items") or [],
            complete=bool(payload.get("complete", False)),
            total=payload.get("total"),
            cursor=payload.get("cursor"),
        )

    def __retype__(self, type_: Any) -> Results[Any]:
        """Coerce the rows to the declared item type, keeping the coverage.

        Replay decodes a step's output against its declared return type, which
        for a paged read is ``Results[Issue]``. Validating straight through
        would rebuild the rows as models and reset the coverage to "unknown";
        skipping validation would keep the coverage and leave raw dicts where
        the workflow expects models. Both are wrong on replay only, which is
        the hardest place to notice.
        """
        from typing import get_args

        from pydantic import TypeAdapter

        args = get_args(type_)
        rows: list[Any] = list(self)
        if args:
            try:
                rows = TypeAdapter(list[args[0]]).validate_python(rows)
            except Exception:
                # A declared type that no longer matches the journal must not
                # destroy an in-flight run — same rule as the generic path.
                rows = list(self)
        return type(self)(
            rows, complete=self.complete, total=self.total, cursor=self.cursor
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        """Validate as ``list[T]``, then wrap.

        Without this, ``Results[Issue]`` is a type pydantic cannot describe, so
        every schema built from a signature — the tool contract the agent
        reads, the fakes the smoke test runs against — quietly degrades to
        ``Any`` and stops carrying the row shape.

        A validated value comes back with ``complete=False``: it was rebuilt
        from data, and data does not know whether the fetch that produced it
        saw everything. Defaulting to ``True`` would manufacture a claim at the
        exact boundary where the evidence was lost.
        """
        from typing import get_args

        from pydantic_core import core_schema

        args = get_args(source)
        item = handler.generate_schema(args[0]) if args else core_schema.any_schema()
        return core_schema.no_info_after_validator_function(
            lambda value: cls(value, complete=False),
            core_schema.list_schema(item),
        )

    @property
    def truncated(self) -> bool:
        return not self.complete

    def summary(self) -> str:
        """A phrase for a report, honest about what it covers.

        Exists so the caller does not have to compose one, and so every caller
        composes the same one — "50 results" and "50 of 312" are different
        claims and the difference is the whole point.
        """
        if self.complete:
            return f"{len(self)} result{'' if len(self) == 1 else 's'}"
        if self.total is not None:
            return f"{len(self)} of {self.total}"
        return f"first {len(self)} (more available)"


def paginates(fn: Any) -> bool:
    """Does this operation return a page of something larger?

    Answered from the return annotation, which is the only place it is written
    down. A ``pagination=True`` maintained by hand beside the implementation is
    a second source of truth, and across a thousand toolsets a second source of
    truth is a guarantee of drift rather than a risk of it.

    Reads the annotation as text as well as as an object, because a module
    using postponed annotations has strings where the class would be and
    importing every toolset to resolve them is exactly the cost the lazy
    registry exists to avoid.
    """
    import inspect

    target = getattr(fn, "fn", fn)
    try:
        annotation = inspect.signature(target).return_annotation
    except (TypeError, ValueError):
        return False
    if annotation is Results:
        return True
    return isinstance(annotation, str) and annotation.split("[")[0].strip() == "Results"


async def collect(
    fetch: Callable[[str | None, int], Awaitable[Page]],
    *,
    limit: int,
    page_size: int,
    max_pages: int = MAX_PAGES,
) -> Results:
    """Page through *fetch* until *limit* items, or the source runs out.

    ``fetch(cursor, size)`` gets ``None`` on the first call and whatever cursor
    the previous :class:`Page` returned after that. It asks for at most *size*,
    never more than the caller still needs.

    Returns a :class:`Results` whose ``complete`` is ``True`` only when the
    source was genuinely exhausted — not when the limit or the page ceiling
    stopped it.
    """
    if limit <= 0:
        return Results([], complete=True)

    collected: list[Any] = []
    cursor: str | None = None
    total: int | None = None
    complete = False

    for _ in range(max_pages):
        page = await fetch(cursor, min(page_size, limit - len(collected)))
        if page.total is not None:
            total = page.total
        collected.extend(page.items)

        if page.cursor is None:
            complete = True
            break
        # A cursor with no rows behind it is a server that will keep saying
        # "more" forever. Treat it as the end rather than trusting it.
        if not page.items:
            complete = True
            break
        cursor = page.cursor
        if len(collected) >= limit:
            break

    if len(collected) > limit:
        # More arrived than was asked for, so there is definitely more behind
        # it — the trim itself is the evidence.
        collected = collected[:limit]
        complete = False

    return Results(
        collected,
        complete=complete,
        total=total,
        cursor=None if complete else cursor,
    )

# ---------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------


@runtime_checkable
class PagingStyle(Protocol):
    """How one API expresses "here is a page, here is the next one".

    Three of these cover every endpoint in the shipped toolsets, and each was
    previously re-derived per operation as a closure — seven of them, differing
    in a field name. A style knows a wire format and nothing about any
    particular service; a client knows the service and nothing about looping;
    :func:`collect` knows looping and nothing about either.

    Adding a fourth dialect is a new class, not an edit to anything here.
    """

    def params(self, cursor: str | None, size: int) -> dict[str, Any]:
        """Request parameters asking for one page."""
        ...

    def read(self, response: Any, cursor: str | None, size: int) -> Page:
        """Rows out of *response*, and where the next page starts."""
        ...


def _rows(response: Any, items: str | None) -> list[Any]:
    """The row list, whether it is wrapped in an envelope or not."""
    if items is None:
        return list(response or [])
    return list((response or {}).get(items) or [])


@dataclass(frozen=True)
class TokenPaging:
    """An opaque token names the next page. Gmail, Calendar, Jira's JQL search.

    ``last`` is honoured over ``token`` where an API sends both: Jira returns a
    ``nextPageToken`` alongside ``isLast: true``, and trusting the token there
    fetches an empty page forever.
    """

    items: str | None = None
    size_param: str = "maxResults"
    token_param: str = "pageToken"
    token_field: str = "nextPageToken"
    last_field: str | None = None
    total_field: str | None = None

    def params(self, cursor: str | None, size: int) -> dict[str, Any]:
        asked: dict[str, Any] = {self.size_param: size}
        if cursor:
            asked[self.token_param] = cursor
        return asked

    def read(self, response: Any, cursor: str | None, size: int) -> Page:
        body = response or {}
        finished = bool(self.last_field and body.get(self.last_field))
        return Page(
            items=_rows(body, self.items),
            cursor=None if finished else (body.get(self.token_field) or None),
            total=body.get(self.total_field) if self.total_field else None,
        )


@dataclass(frozen=True)
class CursorPaging:
    """A ``_links.next`` URL carries the cursor as a query parameter.

    The cursor has to be pulled back *out* of that URL: Confluence v2 returns a
    whole relative link, and sending it as ``cursor`` yields an empty page —
    which looks exactly like reaching the end of the data.
    """

    items: str = "results"
    size_param: str = "limit"
    param: str = "cursor"
    link: tuple[str, str] = ("_links", "next")

    def params(self, cursor: str | None, size: int) -> dict[str, Any]:
        asked: dict[str, Any] = {self.size_param: size}
        if cursor:
            asked[self.param] = cursor
        return asked

    def read(self, response: Any, cursor: str | None, size: int) -> Page:
        from urllib.parse import parse_qs, urlparse

        body = response or {}
        outer, inner = self.link
        link = (body.get(outer) or {}).get(inner)
        found = parse_qs(urlparse(link).query).get(self.param) if link else None
        return Page(items=_rows(body, self.items), cursor=found[0] if found else None)


@dataclass(frozen=True)
class OffsetPaging:
    """Row offsets. Jira's comments and user search, Confluence v1 CQL.

    With ``total_field`` the end is known exactly. Without one — a bare array,
    Jira's ``user/search`` — a short page is the only signal, so a full last
    page is indistinguishable from more data and :func:`collect` reports
    ``complete=False`` rather than claiming what it cannot verify.
    """

    items: str | None = None
    start_param: str = "startAt"
    size_param: str = "maxResults"
    total_field: str | None = None
    next_link: tuple[str, str] | None = None

    def params(self, cursor: str | None, size: int) -> dict[str, Any]:
        return {self.start_param: int(cursor or 0), self.size_param: size}

    def read(self, response: Any, cursor: str | None, size: int) -> Page:
        body = response or {}
        rows = _rows(body, self.items)
        seen = int(cursor or 0) + len(rows)
        total = body.get(self.total_field) if self.total_field and self.items else None

        if total is not None:
            more = seen < total
        elif self.next_link:
            outer, inner = self.next_link
            more = bool((body.get(outer) or {}).get(inner))
        else:
            more = len(rows) >= size
        return Page(items=rows, cursor=str(seen) if more else None, total=total)


async def page_through(
    request: Callable[[dict[str, Any]], Awaitable[Any]],
    *,
    style: PagingStyle,
    limit: int,
    page_size: int,
    row: Callable[[Any], Any] | None = None,
) -> Results[Any]:
    """Page an endpoint, given one function that performs a request.

    The whole of a client's paging code:

        return await page_through(
            lambda params: self._get("issue/X/comment", **params),
            style=OffsetPaging(items="comments", total_field="total"),
            limit=max_results,
            page_size=100,
            row=_to_comment,
        )
    """

    async def fetch(cursor: str | None, size: int) -> Page:
        page = style.read(await request(style.params(cursor, size)), cursor, size)
        if row is None:
            return page
        return Page(
            items=[row(item) for item in page.items],
            cursor=page.cursor,
            total=page.total,
        )

    return await collect(fetch, limit=limit, page_size=page_size)

