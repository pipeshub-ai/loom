"""Errors from Microsoft Graph, split by whether retrying could ever help.

Graph is unusually explicit that the machine-readable part of a failure is
``error.code`` and not the status line or the message:

    You should only code against error codes returned in ``code`` properties.
    […] Don't take any dependency on the content of [message].

So :func:`classify` reads the status first and ``error.code`` second, and the
code is what breaks the ties the status cannot — OneDrive returns
``activityLimitReached`` as a 503 about as often as a 429, and both mean "back
off", not "this request was wrong".

LOOM already knows how to stop: :data:`loom.core.retry.PERMANENT_ERRORS`
includes ``NonRetryableError``, so classifying here is enough to get the right
behaviour out of an ordinary ``Retry`` policy with no per-step configuration.
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError

__all__ = [
    "GraphAPIError",
    "GraphAuthError",
    "GraphPermanentError",
    "GraphThrottled",
    "classify",
]

#: Codes that mean "back off", whatever status carried them. OneDrive and
#: SharePoint both report load shedding as ``activityLimitReached`` under a 503
#: as readily as under a 429.
_RETRYABLE_CODES = frozenset(
    {
        "activitylimitreached",
        "servicenotavailable",
        "unknownerror",
        "generalexception",
    }
)

#: Codes that will fail identically however many times they are sent, even
#: under a status that is usually worth retrying.
_PERMANENT_CODES = frozenset(
    {
        "accessdenied",
        "invalidrequest",
        "itemnotfound",
        "malwaredetected",
        "namealreadyexists",
        "notallowed",
        "notsupported",
        "quotalimitreached",
        "resourcelocked",
        "resyncrequired",
    }
)

#: Statuses that are transient by definition. 509 is Bandwidth Limit Exceeded,
#: which the errors reference says an app "can retry […] after more time has
#: elapsed"; 502/500 are named as retry-worthy by the upload-session page.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 509})


class GraphAPIError(WorkflowError):
    """A Microsoft Graph call failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "",
        url: str = "",
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.url = url
        #: Graph stamps every response with one. Quoting it is what makes a
        #: support ticket answerable, so it is carried rather than dropped.
        self.request_id = request_id


class GraphPermanentError(GraphAPIError, NonRetryableError):
    """A request that will fail the same way however many times it is sent.

    A malformed query, a missing item, a permission the token does not carry.
    """


class GraphAuthError(GraphPermanentError):
    """Credentials are missing, malformed, expired beyond refresh, or revoked."""


class GraphThrottled(GraphAPIError):  # noqa: N818 - reads as a state
    """Throttled. Retryable, and ``retry_after`` says how long to wait.

    Graph's guidance is that honouring ``Retry-After`` "is the fastest way to
    recover from throttling because Microsoft Graph continues to log resource
    usage while a client is being throttled" — that is, retrying sooner makes
    the throttling last longer.
    """

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


def classify(
    status: int,
    body: Any,
    url: str = "",
    headers: dict[str, str] | None = None,
) -> GraphAPIError:
    """Turn an HTTP failure into the most specific error that fits.

    ``headers`` is read for ``Retry-After``, which is also what decides a 409:
    the errors reference says a concurrency-violation 409 "can [be repeated]
    after some delay […] If a Retry-After header is present, that value can be
    used", while a driveItem 409 is ``nameAlreadyExists`` and will never
    succeed on repeat. The header is the only thing that tells them apart.
    """
    head = {k.lower(): v for k, v in (headers or {}).items()}
    detail: dict[str, Any] = body if isinstance(body, dict) else {}
    raw = detail.get("error")
    error: dict[str, Any] = raw if isinstance(raw, dict) else {}

    code = str(error.get("code") or "")
    message = str(error.get("message") or detail.get("error_description") or body)[:500]

    inner = error.get("innerError")
    inner_dict: dict[str, Any] = inner if isinstance(inner, dict) else {}
    # A nested code is more specific than the top-level one, and the docs say
    # to "loop through all the nested error codes […] and use the most detailed
    # one that they understand".
    inner_code = str(inner_dict.get("code") or "")
    request_id = str(inner_dict.get("request-id") or head.get("request-id") or "")

    retry_after = _seconds(head.get("retry-after"))
    described = f"Microsoft Graph {status}: {message}" + (f" [{code}]" if code else "")
    fields: dict[str, Any] = {
        "status": status,
        "code": code,
        "url": url,
        "request_id": request_id,
    }

    lowered = {code.lower(), inner_code.lower()} - {""}

    if lowered & _PERMANENT_CODES:
        return GraphPermanentError(described, **fields)
    if status == 429 or lowered & _RETRYABLE_CODES:
        return GraphThrottled(described, retry_after=retry_after, **fields)
    if status == 401:
        return GraphAuthError(described, **fields)
    if status == 409 and retry_after:
        return GraphThrottled(described, retry_after=retry_after, **fields)
    if status in _RETRYABLE_STATUS:
        return GraphAPIError(described, **fields)
    if 400 <= status < 500:
        # Everything else in the 4xx range, including 403 (permission or
        # licence), 404, 423 Locked and 507 Insufficient Storage. A lock is
        # transient in principle, but in minutes-to-hours, while a retry budget
        # is two attempts over seconds — spending it reports the same thing
        # later with the cause buried under three tracebacks.
        return GraphPermanentError(described, **fields)
    return GraphAPIError(described, **fields)


def _seconds(value: str | None) -> float:
    """Read a ``Retry-After`` header, which Graph always sends as seconds."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
