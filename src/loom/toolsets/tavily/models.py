"""Typed shapes for the Tavily API.

Tavily already speaks snake_case, so these models sit close to the wire. The
one deliberate rename is ``content`` to ``snippet``: Tavily's ``content`` is a
short relevance-ranked excerpt while ``raw_content`` is the whole page, and a
field called ``content`` sitting next to one called ``raw_content`` invites
exactly the wrong reading of which is which.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _text(value: Any) -> str:
    return str(value) if value not in (None, "") else ""


class TavilyResult(BaseModel):
    """One page Tavily matched."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    """Tavily's ``content``: a short, relevance-ranked excerpt. This is what a
    search returns by default and what most workflows should feed a model."""
    score: float | None = None
    """Relevance, 0-1."""
    raw_content: str = ""
    """The whole page — only when the call asked for it."""
    published_date: str = ""
    """Only populated for ``topic="news"``; general search does not carry it."""
    favicon: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> TavilyResult:
        raw = raw or {}
        score = raw.get("score")
        return cls(
            title=_text(raw.get("title")),
            url=_text(raw.get("url")),
            snippet=_text(raw.get("content")),
            score=float(score) if isinstance(score, int | float) else None,
            raw_content=_text(raw.get("raw_content")),
            published_date=_text(raw.get("published_date")),
            favicon=_text(raw.get("favicon")),
        )


class TavilySearch(BaseModel):
    """The answer to a search: the pages, and optionally a written answer.

    A wrapper rather than a bare list because ``include_answer`` is Tavily's
    distinguishing feature — the answer arrives *alongside* the results, and
    returning only the list would throw away the thing the caller asked for.
    """

    query: str = ""
    answer: str = ""
    """An LLM answer to the query — only when ``include_answer`` asked for one."""
    results: list[TavilyResult] = []
    images: list[str] = []
    response_time: float | None = None
    request_id: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> TavilySearch:
        raw = raw or {}
        elapsed = raw.get("response_time")
        return cls(
            query=_text(raw.get("query")),
            answer=_text(raw.get("answer")),
            results=[TavilyResult.from_api(r) for r in (raw.get("results") or [])],
            # Images arrive as bare strings, or as objects when descriptions
            # were requested. Normalising to URLs keeps one return type.
            images=[_image_url(i) for i in (raw.get("images") or [])],
            response_time=float(elapsed) if isinstance(elapsed, int | float) else None,
            request_id=_text(raw.get("request_id")),
        )


class TavilyPage(BaseModel):
    """One page that ``/extract`` successfully read."""

    url: str = ""
    raw_content: str = ""
    images: list[str] = []
    favicon: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> TavilyPage:
        raw = raw or {}
        return cls(
            url=_text(raw.get("url")),
            raw_content=_text(raw.get("raw_content")),
            images=[_image_url(i) for i in (raw.get("images") or [])],
            favicon=_text(raw.get("favicon")),
        )


class TavilyFailure(BaseModel):
    """One URL ``/extract`` could not read, and why."""

    url: str = ""
    error: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> TavilyFailure:
        raw = raw or {}
        return cls(url=_text(raw.get("url")), error=_text(raw.get("error")))


class TavilyExtraction(BaseModel):
    """The answer to an ``/extract`` call: what was read, and what was not.

    ``failed`` is carried rather than dropped for the same reason Exa's
    statuses are: this endpoint returns 200 for a request in which some URLs
    failed, so a caller reading only ``pages`` sees a short list and no
    indication that it asked for more.
    """

    pages: list[TavilyPage] = []
    failed: list[TavilyFailure] = []
    response_time: float | None = None
    request_id: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> TavilyExtraction:
        raw = raw or {}
        elapsed = raw.get("response_time")
        return cls(
            pages=[TavilyPage.from_api(p) for p in (raw.get("results") or [])],
            failed=[
                TavilyFailure.from_api(f) for f in (raw.get("failed_results") or [])
            ],
            response_time=float(elapsed) if isinstance(elapsed, int | float) else None,
            request_id=_text(raw.get("request_id")),
        )


class TavilySiteMap(BaseModel):
    """The URLs discovered under a site."""

    base_url: str = ""
    urls: list[str] = []
    response_time: float | None = None
    request_id: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> TavilySiteMap:
        raw = raw or {}
        elapsed = raw.get("response_time")
        return cls(
            base_url=_text(raw.get("base_url")),
            urls=[str(u) for u in (raw.get("results") or [])],
            response_time=float(elapsed) if isinstance(elapsed, int | float) else None,
            request_id=_text(raw.get("request_id")),
        )


def _image_url(item: Any) -> str:
    """An image entry as a URL, whether or not descriptions were requested."""
    if isinstance(item, dict):
        return _text(item.get("url"))
    return _text(item)
