"""Typed shapes for DuckDuckGo results.

``ddgs`` hands back bare dicts whose keys differ per category — a web result
uses ``href`` and ``body`` while a news result uses ``url`` and ``body`` and an
image uses ``image`` and ``url``-as-the-page. These models normalise that to one
vocabulary (``url``, ``snippet``) so a workflow reading a web result and a news
result is reading the same field names, and so a key rename upstream is a
one-line change here rather than a change in every caller.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _text(value: Any) -> str:
    return str(value) if value not in (None, "") else ""


class DuckDuckGoResult(BaseModel):
    """One web result."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    """``ddgs`` calls this ``body``: the excerpt shown under the link."""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> DuckDuckGoResult:
        raw = raw or {}
        return cls(
            title=_text(raw.get("title")),
            # Web results carry `href`; every other category uses `url`.
            url=_text(raw.get("href") or raw.get("url")),
            snippet=_text(raw.get("body")),
        )


class DuckDuckGoNews(BaseModel):
    """One news result."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    date: str = ""
    """ISO 8601 publication time, when the source gave one."""
    source: str = ""
    """The publication's name."""
    image: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> DuckDuckGoNews:
        raw = raw or {}
        return cls(
            title=_text(raw.get("title")),
            url=_text(raw.get("url") or raw.get("href")),
            snippet=_text(raw.get("body")),
            date=_text(raw.get("date")),
            source=_text(raw.get("source")),
            image=_text(raw.get("image")),
        )


class DuckDuckGoImage(BaseModel):
    """One image result."""

    title: str = ""
    image: str = ""
    """Direct URL of the image itself."""
    thumbnail: str = ""
    url: str = ""
    """The page the image appears on — not the image."""
    source: str = ""
    width: int | None = None
    height: int | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> DuckDuckGoImage:
        raw = raw or {}
        return cls(
            title=_text(raw.get("title")),
            image=_text(raw.get("image")),
            thumbnail=_text(raw.get("thumbnail")),
            url=_text(raw.get("url")),
            source=_text(raw.get("source")),
            width=_int(raw.get("width")),
            height=_int(raw.get("height")),
        )


def _int(value: Any) -> int | None:
    """Dimensions arrive as strings often enough to be worth coercing."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
