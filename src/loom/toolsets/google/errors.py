"""Errors from the Google APIs, split by whether retrying could ever help.

The distinction is the point. A 429 or a 503 is worth another attempt; a 400 or
a 403 will fail identically every time, and retrying it three times with backoff
just makes a workflow take longer to report the same thing. LOOM already knows
how to stop — :data:`loom.core.retry.PERMANENT_ERRORS` includes
``NonRetryableError`` — so classifying here is enough to get the right behaviour
from an ordinary ``Retry`` policy, with no per-step configuration.
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError

__all__ = [
    "GmailHistoryExpired",
    "GoogleAPIError",
    "GoogleAuthError",
    "GooglePermanentError",
    "GoogleRateLimited",
    "classify",
]


class GoogleAPIError(WorkflowError):
    """A Google API call failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        reason: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.url = url


class GooglePermanentError(GoogleAPIError, NonRetryableError):
    """A request that will fail the same way however many times it is sent.

    Bad arguments, a missing message, a scope the token does not carry.
    """


class GoogleAuthError(GooglePermanentError):
    """Credentials are missing, malformed, expired beyond refresh, or revoked."""


class GoogleRateLimited(GoogleAPIError):  # noqa: N818 - reads as a state
    """Quota exceeded. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


def classify(status: int, body: Any, url: str = "") -> GoogleAPIError:
    """Turn an HTTP failure into the most specific error that fits.

    Google reports the useful part of a failure in a nested ``error`` object
    rather than the status line — a 403 is quota exhaustion or a missing scope
    or a suspended account, and only ``reason`` tells them apart. So the reason
    is what decides retryability where the status alone is ambiguous.
    """
    detail: dict[str, Any] = body if isinstance(body, dict) else {}
    raw = detail.get("error")
    error: dict[str, Any] = raw if isinstance(raw, dict) else {}
    message = str(error.get("message") or detail.get("error_description") or body)[:500]
    reasons = [
        str(item.get("reason", ""))
        for item in error.get("errors", [])
        if isinstance(item, dict)
    ]
    reason = reasons[0] if reasons else str(detail.get("error", "")) or ""

    described = f"Google API {status}: {message}" + (f" [{reason}]" if reason else "")

    if status == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return GoogleRateLimited(described, status=status, reason=reason, url=url)
    if status == 401 or reason in {"authError", "invalid_grant", "invalid_client"}:
        return GoogleAuthError(described, status=status, reason=reason, url=url)
    if status == 403:
        # The one 403 that is worth retrying: a per-minute quota, not a
        # permission the token is never going to acquire by waiting.
        if reason in {"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
            return GoogleRateLimited(described, status=status, reason=reason, url=url)
        return GooglePermanentError(described, status=status, reason=reason, url=url)
    if 400 <= status < 500:
        return GooglePermanentError(described, status=status, reason=reason, url=url)
    return GoogleAPIError(described, status=status, reason=reason, url=url)


class GmailHistoryExpired(GooglePermanentError):  # noqa: N818 - names the state
    """A Gmail history id is older than Gmail still holds.

    Its own type because it is not a failure to be retried, and it is not
    "nothing happened" either — it is *we cannot say what happened*, which is
    the one answer a caller must handle rather than log. The recovery is a full
    resync plus a recorded gap, and nothing else produces the same obligation.
    """
