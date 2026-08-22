"""Async GitLab REST API v4 client — pure httpx, no vendor SDK.

Credentials and host resolve from arguments, then the environment:

    GITLAB_TOKEN        a personal, project, or group access token
    GITLAB_OAUTH_TOKEN  an OAuth access token
    GITLAB_URL          the instance; defaults to https://gitlab.com

**The host is configuration.** GitLab is self-managed as often as not, so there
is no constant base URL — the same situation as Salesforce, minus the discovery
step, because the operator already knows their instance.

**The two token kinds use different header names**: ``PRIVATE-TOKEN`` for
access tokens, ``Authorization: Bearer`` for OAuth. Different names rather than
different values, so unlike ClickUp this cannot be subtly wrong — it either
matches or it does not.

Paging is offset, signalled in response headers, so the request callable
returns ``{"items": rows, "headers": {...}}`` and
:class:`~loom.toolsets.pagination.HeaderPaging` reads ``x-next-page``. That
header is empty on the last page, which means the same as absent.

``x-total`` is read when present and left unknown when not: GitLab omits it
past 10,000 records — exactly when a total matters — and reading a missing
header as zero would report the largest result sets as empty.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.gitlab.models import (
    GitLabIssue,
    GitLabMergeRequest,
    GitLabNote,
    GitLabProject,
    GitLabUser,
)
from loom.toolsets.pagination import HeaderPaging, Results, page_through

DEFAULT_URL = "https://gitlab.com"

#: The API generation. v4 has been the only one for years, which is exactly why
#: it was a literal in every path — and why a v5 would otherwise be a library
#: change rather than a configuration one.
DEFAULT_API_VERSION = "v4"

#: GitLab's ceiling; it defaults to 20, which is rarely what a workflow wants.
GITLAB_PAGE_CAP = 100


class GitLabError(WorkflowError):
    """A GitLab request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0, **kw: Any) -> None:
        super().__init__(message)
        self.status = status


class GitLabPermanentError(GitLabError, NonRetryableError):
    """Fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class GitLabAuthError(GitLabPermanentError):
    """Missing, malformed, or revoked credentials, or a missing scope."""


class GitLabNotFound(GitLabPermanentError):  # noqa: N818 - names a state
    """No such resource — **or** one this token may not see.

    Its own class for the same reason as GitHub's: GitLab returns 404 rather
    than 403 for a private project, so the status alone cannot tell "no such
    thing" from "not yours", and a permissions problem gets debugged as a typo.
    """


class GitLabRateLimited(GitLabError):  # noqa: N818 - names a state
    """Too many requests. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class GitLabClient:
    """Thin async wrapper around the GitLab REST API v4."""

    def __init__(
        self,
        token: str = "",
        *,
        oauth_token: str = "",
        base_url: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._oauth = oauth_token
        self._base_url = (
            base_url or DEFAULT_URL
        ).rstrip("/")
        self._api_version = api_version
        self._timeout = timeout

        if not self._token and not self._oauth:
            raise GitLabAuthError(
                "GitLab needs a token: set GITLAB_TOKEN (a personal, project, "
                "or group access token) or GITLAB_OAUTH_TOKEN, or pass "
                "token=. Set GITLAB_URL too for a self-managed instance; it "
                "defaults to https://gitlab.com."
            )

    # -- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Different header *names*, not different values — so a token in the
        # wrong slot fails loudly rather than looking almost right.
        if self._oauth:
            return {"Authorization": f"Bearer {self._oauth}"}
        return {"PRIVATE-TOKEN": self._token}

    async def _envelope(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        """A response as plain data: the body, plus the headers paging needs."""
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self._base_url}/api/{self._api_version}{path}",
                headers={**self._headers(), "Content-Type": "application/json"},
                params=_clean(params),
                json=json,
            )
        if response.status_code >= 400:
            raise _classify(response)
        body: Any = {}
        if response.status_code != 204 and response.content:
            body = response.json()
        return {"items": body, "headers": dict(response.headers)}

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> Any:
        envelope = await self._envelope(method, path, params=params, json=json)
        return envelope["items"]

    async def _paged(
        self, path: str, *, params: dict[str, Any] | None = None, limit: int, row: Any
    ) -> Results[Any]:
        async def request(asked: dict[str, Any]) -> Any:
            return await self._envelope(
                "GET", path, params={**(params or {}), **asked}
            )

        return await page_through(
            request,
            style=_GITLAB_PAGING,
            limit=limit,
            page_size=GITLAB_PAGE_CAP,
            row=row,
        )

    @staticmethod
    def _project(project: str) -> str:
        """A project id, or a URL-encoded ``group/project`` path.

        GitLab accepts either, but the path must be encoded — an unencoded
        slash makes it a different route and returns 404, which reads as a
        missing project rather than as a missing ``%2F``.
        """
        return project if str(project).isdigit() else quote(str(project), safe="")

    # -- identity and projects ---------------------------------------------

    async def whoami(self) -> GitLabUser:
        return GitLabUser.from_api(await self._request("GET", "/user"))

    async def find_users(self, query: str, *, limit: int = 20) -> list[GitLabUser]:
        """Find people by username, name, or email.

        The join between what someone is called and the numeric id every
        assignment takes.
        """
        rows = await self._request(
            "GET",
            "/users",
            params={"search": query, "per_page": min(limit, GITLAB_PAGE_CAP)},
        )
        return [GitLabUser.from_api(u) for u in (rows or [])]

    async def list_projects(
        self,
        *,
        search: str = "",
        membership: bool = True,
        limit: int = 50,
        order_by: str = "last_activity_at",
    ) -> Results[GitLabProject]:
        return await self._paged(
            "/projects",
            params={
                "search": search or None,
                "membership": membership,
                "order_by": order_by,
            },
            limit=limit,
            row=GitLabProject.from_api,
        )

    async def get_project(self, project: str) -> GitLabProject:
        return GitLabProject.from_api(
            await self._request("GET", f"/projects/{self._project(project)}")
        )

    # -- issues -------------------------------------------------------------

    async def list_issues(
        self,
        project: str,
        *,
        state: str = "opened",
        labels: str = "",
        assignee: str = "",
        limit: int = 50,
    ) -> Results[GitLabIssue]:
        return await self._paged(
            f"/projects/{self._project(project)}/issues",
            params={
                # GitLab says "opened", not "open" — the GitHub word returns
                # everything, silently, because an unknown state is ignored.
                "state": state,
                "labels": labels or None,
                "assignee_username": assignee or None,
            },
            limit=limit,
            row=GitLabIssue.from_api,
        )

    async def get_issue(self, project: str, iid: int) -> GitLabIssue:
        return GitLabIssue.from_api(
            await self._request(
                "GET", f"/projects/{self._project(project)}/issues/{iid}"
            )
        )

    async def create_issue(
        self,
        project: str,
        title: str,
        *,
        description: str = "",
        labels: list[str] | None = None,
        assignee_ids: list[str] | None = None,
    ) -> GitLabIssue:
        payload: dict[str, Any] = {"title": title}
        if description:
            payload["description"] = description
        if labels:
            payload["labels"] = ",".join(labels)
        if assignee_ids:
            payload["assignee_ids"] = [int(a) for a in assignee_ids if str(a).isdigit()]
        return GitLabIssue.from_api(
            await self._request(
                "POST", f"/projects/{self._project(project)}/issues", json=payload
            )
        )

    async def update_issue(
        self,
        project: str,
        iid: int,
        *,
        title: str = "",
        description: str = "",
        state_event: str = "",
        labels: list[str] | None = None,
    ) -> GitLabIssue:
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if state_event:
            # `state_event`, not `state`: GitLab takes the *transition*
            # ("close", "reopen"), and sending a state silently changes nothing.
            payload["state_event"] = state_event
        if labels is not None:
            payload["labels"] = ",".join(labels)
        return GitLabIssue.from_api(
            await self._request(
                "PUT",
                f"/projects/{self._project(project)}/issues/{iid}",
                json=payload,
            )
        )

    async def list_issue_notes(
        self, project: str, iid: int, *, limit: int = 50, include_system: bool = False
    ) -> Results[GitLabNote]:
        """Notes on an issue, system records excluded by default.

        GitLab records "changed the milestone" as a note too, and "show me the
        comments" does not mean that.
        """
        rows = await self._paged(
            f"/projects/{self._project(project)}/issues/{iid}/notes",
            limit=limit,
            row=GitLabNote.from_api,
        )
        if include_system:
            return rows
        return Results(
            [note for note in rows if not note.system],
            complete=rows.complete,
            cursor=rows.cursor,
        )

    async def add_issue_note(self, project: str, iid: int, body: str) -> GitLabNote:
        return GitLabNote.from_api(
            await self._request(
                "POST",
                f"/projects/{self._project(project)}/issues/{iid}/notes",
                json={"body": body},
            )
        )

    # -- merge requests -----------------------------------------------------

    async def list_merge_requests(
        self,
        project: str,
        *,
        state: str = "opened",
        target_branch: str = "",
        limit: int = 50,
    ) -> Results[GitLabMergeRequest]:
        return await self._paged(
            f"/projects/{self._project(project)}/merge_requests",
            params={"state": state, "target_branch": target_branch or None},
            limit=limit,
            row=GitLabMergeRequest.from_api,
        )

    async def get_merge_request(self, project: str, iid: int) -> GitLabMergeRequest:
        return GitLabMergeRequest.from_api(
            await self._request(
                "GET", f"/projects/{self._project(project)}/merge_requests/{iid}"
            )
        )

    async def create_merge_request(
        self,
        project: str,
        title: str,
        *,
        source_branch: str,
        target_branch: str,
        description: str = "",
        draft: bool = False,
    ) -> GitLabMergeRequest:
        return GitLabMergeRequest.from_api(
            await self._request(
                "POST",
                f"/projects/{self._project(project)}/merge_requests",
                json={
                    "title": f"Draft: {title}" if draft else title,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "description": description,
                },
            )
        )


#: GitLab names the next page directly, and empty means the last one.
_GITLAB_PAGING = HeaderPaging(
    items="items",
    page_param="page",
    size_param="per_page",
    next_header="x-next-page",
    total_header="x-total",
)


def _clean(params: Any) -> Any:
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> GitLabError:
    """Turn a failed response into the narrowest error that fits."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = None
    if isinstance(body, dict):
        # GitLab uses `message` on some endpoints and `error` on others.
        detail = body.get("message") or body.get("error")
        if isinstance(detail, dict | list):
            detail = str(detail)
    message = f"GitLab {status}: {detail or response.text[:200] or 'request failed'}"
    headers = {str(k).lower(): v for k, v in dict(response.headers).items()}

    if status == 429:
        return GitLabRateLimited(
            message,
            status=status,
            retry_after=float(headers.get("retry-after", 0) or 0),
        )
    if status == 401:
        return GitLabAuthError(message, status=status)
    if status == 404:
        return GitLabNotFound(
            f"{message} — GitLab returns 404 for a project that exists but "
            "this token cannot see, so check the token's scopes as well as the "
            "path. A group/project path must be URL-encoded.",
            status=status,
        )
    if 400 <= status < 500:
        return GitLabPermanentError(message, status=status)
    return GitLabError(message, status=status)


