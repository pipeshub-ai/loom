"""Slack failures, which do not arrive as HTTP status codes.

This is the one thing to know before writing anything against the Slack Web
API. A failure is an **HTTP 200** carrying::

    {"ok": false, "error": "channel_not_found"}

So a client written to the shape every other toolset here uses — raise above
399, otherwise decode — treats every Slack failure as a success. A workflow
posting to a channel the app was never invited to would report that it sent the
message, and deliver nothing. That is precisely the *fewer rows and no error*
class this codebase exists to be careful about, and it is the default here
rather than an edge case.

So classification reads the ``error`` **string**, not the status line. The split
is the same one that matters everywhere: could sending this again ever produce a
different answer?
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError

__all__ = [
    "SlackAPIError",
    "SlackAuthError",
    "SlackMissingScope",
    "SlackPermanentError",
    "SlackRateLimited",
    "classify",
    "raise_for_status",
]

#: Slack-side problems. The same call may well work in a moment.
_RETRYABLE = frozenset(
    {
        "ratelimited",
        "rate_limited",
        "service_unavailable",
        "internal_error",
        "fatal_error",
        "request_timeout",
    }
)

#: The credential itself is wrong, revoked, or expired. Retrying cannot fix it;
#: a human reauthorizing can.
_AUTH = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_expired",
        "token_revoked",
        "no_permission",
        "ekm_access_denied",
    }
)

#: The app was installed without a scope this call needs.
_SCOPE = frozenset({"missing_scope", "not_allowed_token_type"})


class SlackAPIError(WorkflowError):
    """A Slack call failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        error: str = "",
        method: str = "",
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        """Slack's machine-readable code, e.g. ``channel_not_found``."""
        self.method = method
        self.response = response or {}


class SlackPermanentError(SlackAPIError, NonRetryableError):
    """A failure that will repeat identically however many times it is sent.

    A channel that does not exist, a message id that is wrong, an argument
    Slack rejected. Raised as a ``NonRetryableError`` so an ordinary ``Retry``
    policy stops rather than sleeping through three attempts at the same answer.
    """


class SlackAuthError(SlackPermanentError):
    """The token is missing, invalid, expired, or revoked."""


class SlackMissingScope(SlackPermanentError):  # noqa: N818 - names the condition
    """The app is installed without a scope this call needs.

    Its own type because the fix is different in kind: not a corrected argument
    but a reinstall with wider permissions, done by a person, once. Slack names
    the scope it wanted, so the message can say exactly which one.
    """

    def __init__(self, message: str, *, needed: str = "", **kw: Any) -> None:
        super().__init__(message, **kw)
        self.needed = needed


class SlackRateLimited(SlackAPIError):  # noqa: N818 - reads as a state
    """Too many calls. Retryable, and ``retry_after`` says how long to wait."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


def classify(
    body: dict[str, Any],
    *,
    method: str = "",
    status: int = 200,
    retry_after: float = 0.0,
) -> SlackAPIError:
    """Turn an ``ok: false`` body into the most specific error that fits.

    *status* and *retry_after* are only consulted for the one failure Slack does
    surface as HTTP — a 429, which arrives with a ``Retry-After`` header and
    sometimes an empty body.
    """
    error = str(body.get("error", "")) or (f"http_{status}" if status >= 400 else "")
    described = f"Slack {method or 'API'} failed: {error or 'unknown error'}"

    if status == 429 or error in _RETRYABLE:
        return SlackRateLimited(
            described, error=error, method=method, response=body,
            retry_after=retry_after,
        )
    if error in _SCOPE:
        needed = str(body.get("needed", ""))
        return SlackMissingScope(
            f"{described}"
            + (f" — the app needs the '{needed}' scope" if needed else "")
            + ". Reinstall the app with that scope, then 'loom connect slack'.",
            needed=needed, error=error, method=method, response=body,
        )
    if error in _AUTH:
        return SlackAuthError(
            f"{described}. Run 'loom connect slack' to reauthorize.",
            error=error, method=method, response=body,
        )
    if status >= 500:
        # A genuine Slack outage, which is the one case where the status line
        # carries the answer and no body is guaranteed.
        return SlackAPIError(described, error=error, method=method, response=body)
    return SlackPermanentError(described, error=error, method=method, response=body)


def raise_for_status(
    body: dict[str, Any],
    *,
    method: str = "",
    status: int = 200,
    retry_after: float = 0.0,
) -> dict[str, Any]:
    """Return *body* when Slack said ``ok``, else raise the classified error.

    Every response goes through here. A caller that forgets is a caller that
    silently treats a failure as an empty result.
    """
    if status < 400 and body.get("ok"):
        return body
    raise classify(body, method=method, status=status, retry_after=retry_after)
