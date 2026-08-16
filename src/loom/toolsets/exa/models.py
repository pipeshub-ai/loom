"""Typed shapes for the Exa API.

Exa speaks camelCase on the wire (``numResults``, ``publishedDate``); these
models are snake_case, and ``from_api`` is the only place the two meet. A model
that mirrored the wire casing would push it into every workflow that reads a
field.

Every field is optional. Exa returns ``text``, ``highlights``, and ``summary``
only when the request asked for them, so a result built from a bare search has
a title and a URL and little else — the defaults here say "not requested",
never "empty at the source".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _text(value: Any) -> str:
    """A wire value as a string, treating null and missing alike."""
    return str(value) if value not in (None, "") else ""


class ExaResult(BaseModel):
    """One page Exa matched."""

    id: str = ""
    """Exa's own document id. Accepted by ``/contents`` in place of a URL."""
    url: str = ""
    title: str = ""
    published_date: str = ""
    """ISO 8601, when Exa knows it. Absent for a great many pages."""
    author: str = ""
    score: float | None = None
    """Relevance, for a neural search. ``None`` for keyword-style searches."""
    text: str = ""
    """Full page text — only when the call asked for it."""
    highlights: list[str] = []
    """Query-relevant snippets — only when the call asked for them."""
    summary: str = ""
    """An LLM summary — only when the call asked for one."""
    image: str = ""
    favicon: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> ExaResult:
        raw = raw or {}
        score = raw.get("score")
        return cls(
            id=_text(raw.get("id")),
            url=_text(raw.get("url")),
            title=_text(raw.get("title")),
            published_date=_text(raw.get("publishedDate")),
            author=_text(raw.get("author")),
            score=float(score) if isinstance(score, int | float) else None,
            text=_text(raw.get("text")),
            highlights=[str(h) for h in (raw.get("highlights") or [])],
            summary=_text(raw.get("summary")),
            image=_text(raw.get("image")),
            favicon=_text(raw.get("favicon")),
        )


class ExaFetchStatus(BaseModel):
    """What happened to one URL a ``/contents`` call asked for.

    Its own type because ``/contents`` answers **per URL**: a request for ten
    pages can return eight and two failures, with a 200 for the whole call. A
    caller that reads only ``results`` sees eight pages and no indication that
    it asked for ten — the same shape of silent shortfall that ``Results``
    exists to prevent for paging.
    """

    id: str = ""
    """The URL (or Exa id) this status is about."""
    status: str = ""
    """``success`` or ``error``."""
    source: str = ""
    """``cached`` or ``crawled``, when Exa says."""
    error_tag: str = ""
    """Exa's machine-readable reason, e.g. ``CRAWL_NOT_FOUND``."""
    http_status: int | None = None
    """The status the crawl itself got, when there was one."""

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> ExaFetchStatus:
        raw = raw or {}
        error = raw.get("error") or {}
        code = error.get("httpStatusCode")
        return cls(
            id=_text(raw.get("id")),
            status=_text(raw.get("status")),
            source=_text(raw.get("source")),
            error_tag=_text(error.get("tag")),
            http_status=int(code) if isinstance(code, int) else None,
        )


class ExaContents(BaseModel):
    """The answer to a ``/contents`` call: what came back, and what did not."""

    results: list[ExaResult] = []
    statuses: list[ExaFetchStatus] = []
    """One entry per requested URL. Check it before treating ``results`` as
    the whole request — see :class:`ExaFetchStatus`."""
    request_id: str = ""
    cost_dollars: float | None = None
    """What Exa charged for this call, when it says. Usage counter, not an
    invoice — Exa's own wording."""

    @property
    def failed(self) -> list[ExaFetchStatus]:
        """The URLs that did not come back, so a caller can report them."""
        return [s for s in self.statuses if not s.ok]

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> ExaContents:
        raw = raw or {}
        return cls(
            results=[ExaResult.from_api(r) for r in (raw.get("results") or [])],
            statuses=[
                ExaFetchStatus.from_api(s) for s in (raw.get("statuses") or [])
            ],
            request_id=_text(raw.get("requestId")),
            cost_dollars=_cost(raw),
        )


class ExaCitation(BaseModel):
    """A source Exa used to answer a question."""

    id: str = ""
    url: str = ""
    title: str = ""
    published_date: str = ""
    author: str = ""
    text: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> ExaCitation:
        raw = raw or {}
        return cls(
            id=_text(raw.get("id")),
            url=_text(raw.get("url")),
            title=_text(raw.get("title")),
            published_date=_text(raw.get("publishedDate")),
            author=_text(raw.get("author")),
            text=_text(raw.get("text")),
        )


class ExaAnswer(BaseModel):
    """A generated answer, and the pages it came from.

    The citations are the point. An answer without them is a model's opinion
    about the web; with them a workflow can quote a source, and a reviewer can
    check one.
    """

    answer: str = ""
    citations: list[ExaCitation] = []
    cost_dollars: float | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> ExaAnswer:
        raw = raw or {}
        answer = raw.get("answer")
        return cls(
            # `answer` is a string normally and an object when the caller
            # supplied an outputSchema. Stringifying keeps one return type.
            answer=answer if isinstance(answer, str) else _text(answer),
            citations=[ExaCitation.from_api(c) for c in (raw.get("citations") or [])],
            cost_dollars=_cost(raw),
        )


def _cost(raw: dict[str, Any]) -> float | None:
    cost = raw.get("costDollars")
    if isinstance(cost, dict) and isinstance(cost.get("total"), int | float):
        return float(cost["total"])
    return None
