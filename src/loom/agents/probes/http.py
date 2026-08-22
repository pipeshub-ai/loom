"""Look at an HTTP endpoint.

GET and HEAD, and no code path that sends anything else. A probe is handed to a
model, so "read-only" has to be a property of what the class can express rather
than an instruction it is asked to follow.
"""

from __future__ import annotations

import json
from typing import Any

from loom.agents.probes.base import Observation, ProbeError, redirect_note

__all__ = ["HttpProbe"]

#: Enough to characterise a response without becoming the context window.
_BODY_CHARS = 4000


class HttpProbe:
    """Fetch a URL and describe what came back.

    Answers the question a spec cannot: what shape is this API's response, what
    are its field names, does it need auth. Every one of those was previously
    guessed at from a URL, and a guessed field name is a workflow that runs and
    returns ``None``.
    """

    id = "http"

    def __init__(self, *, timeout: float = 20.0, max_bytes: int = 2_000_000) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes

    def supports(self, target: str) -> bool:
        return target.startswith(("http://", "https://"))

    async def observe(self, target: str, *, hint: str = "") -> Observation:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a core dep
            raise ProbeError("httpx is not installed") from exc

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(target, timeout=self._timeout)
        except Exception as exc:
            raise ProbeError(f"could not reach {target}: {exc}") from exc

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        body = response.text[: self._max_bytes]

        shape = _describe(body, content_type)
        # `follow_redirects=True` means the status here is the *last* hop's, so
        # a 200 says nothing about whether this is the URL that was asked for.
        landed = str(response.url)
        note = redirect_note(target, landed)
        summary = (
            f"HTTP {response.status_code} {content_type or 'unknown type'}, "
            f"{len(response.content)} bytes. {shape.headline}"
        )
        return Observation(
            target=target,
            landed=landed,
            summary=f"{note} {summary}" if note else summary,
            detail=shape.detail,
            probe=self.id,
        )


class _Shape:
    __slots__ = ("detail", "headline")

    def __init__(self, headline: str, detail: str) -> None:
        self.headline, self.detail = headline, detail


def _describe(body: str, content_type: str) -> _Shape:
    """What the body *is*, preferring structure over the first N characters.

    A JSON API's field names are the thing being looked for, and they are what
    a truncated body loses first when the payload is a long list.
    """
    if "html" in content_type:
        return _Shape(
            "HTML. If the page renders its content or controls in the browser, "
            "this source will not show them — observe it again with "
            "probe='browser' to see what a user sees.",
            body[:_BODY_CHARS],
        )
    if "json" in content_type:
        try:
            decoded = json.loads(body)
        except ValueError:
            return _Shape("Body is not valid JSON.", body[:_BODY_CHARS])
        return _Shape(
            f"JSON {_kind(decoded)}.",
            json.dumps(_outline(decoded), indent=2)[:_BODY_CHARS],
        )
    return _Shape(f"{len(body)} characters of text.", body[:_BODY_CHARS])


def _kind(value: Any) -> str:
    if isinstance(value, list):
        return f"array of {len(value)}"
    if isinstance(value, dict):
        return f"object with {len(value)} key(s)"
    return type(value).__name__


def _outline(value: Any, *, depth: int = 0) -> Any:
    """The structure, with long collections stood down to their first entry.

    A thousand-element array says the same thing about its shape as its first
    element does, and says it in three orders of magnitude less context.
    """
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {key: _outline(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if not value:
            return []
        return [_outline(value[0], depth=depth + 1), f"… {len(value)} total"][
            : 1 if len(value) == 1 else 2
        ]
    if isinstance(value, str) and len(value) > 120:
        return value[:120] + "…"
    return value
