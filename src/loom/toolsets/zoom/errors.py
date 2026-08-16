"""Zoom failures, split by whether retrying could ever help.

Zoom does use HTTP status codes, unlike Slack — but it also carries a numeric
``code`` in the body that is more specific than the status, and the two
disagree often enough to matter. A ``404`` with body code ``3001`` is "that
meeting does not exist"; a ``404`` with ``1001`` is "that user does not exist";
both are permanent, and neither is worth three attempts with backoff.

The one genuinely ambiguous status is ``429``. Zoom rate-limits per second
*and* per day, and the two need different responses — a per-second limit clears
on its own, a daily one does not clear until midnight UTC and retrying against
it all afternoon achieves nothing. Zoom distinguishes them only in the message
text, so that is what is read.
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError

__all__ = [
    "AUTH_CODES",
    "ZoomAPIError",
    "ZoomAuthError",
    "ZoomDailyLimitReached",
    "ZoomPermanentError",
    "ZoomRateLimited",
    "classify",
]

#: Body codes that mean the credential rather than the request: ``124`` is an
#: invalid or expired access token, ``3000`` is a scope the app does not hold.
AUTH_CODES = frozenset({124, 3000})


class ZoomAPIError(WorkflowError):
    """A Zoom call failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: int = 0,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        """Zoom's own numeric code, which is more specific than the status."""
        self.url = url


class ZoomPermanentError(ZoomAPIError, NonRetryableError):
    """A request that will fail identically however many times it is sent."""


class ZoomAuthError(ZoomPermanentError):
    """Credentials are missing, malformed, revoked, or lack a scope."""


class ZoomRateLimited(ZoomAPIError):  # noqa: N818 - reads as a state
    """Per-second quota exceeded. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ZoomDailyLimitReached(ZoomPermanentError):  # noqa: N818 - reads as a state
    """The daily quota is gone until midnight UTC.

    Deliberately **not** retryable, even though it is a 429 like the other one.
    A per-second limit clears while a step backs off; a daily limit does not,
    and retrying against it burns the rest of the workflow's time to arrive at
    the same answer. Failing now lets the run be rescheduled for tomorrow.
    """


def classify(status: int, body: Any, url: str = "") -> ZoomAPIError:
    """Turn an HTTP failure into the most specific error that fits."""
    detail: dict[str, Any] = body if isinstance(body, dict) else {}
    code = int(detail.get("code", 0) or 0)
    message = str(detail.get("message", "") or body)[:500]
    described = f"Zoom API {status}: {message}" + (f" [code {code}]" if code else "")

    if status == 429:
        # Only the message text tells the two limits apart.
        if "daily" in message.lower():
            return ZoomDailyLimitReached(described, status=status, code=code, url=url)
        return ZoomRateLimited(described, status=status, code=code, url=url)
    if status in (401, 403) or code in AUTH_CODES:
        return ZoomAuthError(described, status=status, code=code, url=url)
    if 400 <= status < 500:
        return ZoomPermanentError(described, status=status, code=code, url=url)
    return ZoomAPIError(described, status=status, code=code, url=url)
