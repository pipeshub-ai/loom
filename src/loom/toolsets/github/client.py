"""Async GitHub REST API client — pure httpx, no vendor SDK.

Credentials resolve from an argument, then the environment:

    GITHUB_TOKEN            a classic or fine-grained personal access token
    GITHUB_API_URL          for GitHub Enterprise Server; defaults to github.com

Paging is a **Link header**, so the request callable returns
``{"items": rows, "headers": {...}}`` and
:class:`~loom.toolsets.pagination.HeaderPaging` reads both halves. The last page
is the one whose header has no ``rel="next"`` — that absence is GitHub's own
end-of-results signal, not a malformed response.

Three things here exist because GitHub reports partial answers without erroring:

**Issue listings contain pull requests.** GitHub's model, not a bug: every PR
is an issue. ``list_issues`` filters them out by default, because a caller
asking for open issues and getting a list half full of PRs has a wrong answer
and a wrong count with nothing to notice.

**Search returns at most 1,000 results** however large ``total_count`` is, so
the total is reported as what was retrievable rather than what exists.

**``incomplete_results``** means the query timed out server-side and, in
GitHub's words, more results might have been found — or might not.
"""

from __future__ import annotations

import os
from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.github.models import (
    GitHubComment,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRepo,
    GitHubUser,
)
from loom.toolsets.pagination import HeaderPaging, Results, page_through

BASE_URL = "https://api.github.com"

#: The dated REST contract this client asks for. Sent on every request, and a
#: constructor argument rather than a constant: GitHub pins behaviour to it, so
#: a host that needs a newer contract — or an Enterprise Server pinned to an
#: older one — must be able to say so without editing the library.
DEFAULT_API_VERSION = "2022-11-28"

#: GitHub silently reduces anything larger rather than erroring.
GITHUB_PAGE_CAP = 100

#: Search will not page past this, whatever ``total_count`` reports.
SEARCH_TOTAL_CAP = 1_000


class GitHubError(WorkflowError):
    """A GitHub request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0, **kw: Any) -> None:
        super().__init__(message)
        self.status = status


class GitHubPermanentError(GitHubError, NonRetryableError):
    """Fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class GitHubAuthError(GitHubPermanentError):
    """Missing, malformed, or revoked credentials, or a missing scope."""


class GitHubNotFound(GitHubPermanentError):  # noqa: N818 - names a state
    """No such resource — **or** one this token may not see.

    Its own class because GitHub deliberately returns 404 rather than 403 for a
    private resource, so "not found" and "not yours" are indistinguishable from
    the status alone. The message says so, or a permissions problem gets
    debugged as a typo.
    """


class GitHubRateLimited(GitHubError):  # noqa: N818 - names a state
    """Rate limited. Retryable, and the caller should back off.

    5,000 requests an hour overall, but **30 a minute for search** — and
    secondary limits cap content creation at 80 a minute, which a bulk
    commenting workflow reaches.
    """

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class GitHubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
    ) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._api_version = api_version
        self._base_url = (
            base_url or os.environ.get("GITHUB_API_URL", BASE_URL)
        ).rstrip("/")
        self._timeout = timeout

        if not self._token:
            raise GitHubAuthError(
                "GitHub needs a token: set GITHUB_TOKEN (a classic or "
                "fine-grained personal access token) or pass token=."
            )

    # -- transport ----------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> Any:
        envelope = await self._envelope(method, path, params=params, json=json)
        return envelope["items"]

    async def _envelope(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        """A response as plain data: the body, plus the headers paging needs.

        Headers are carried explicitly rather than by handing the style an
        httpx object, which would make the paging dialect untestable without a
        transport.
        """
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": self._api_version,
                },
                params=_clean(params),
                json=json,
            )
        if response.status_code >= 400:
            raise _classify(response)
        body: Any = {}
        if response.status_code != 204 and response.content:
            body = response.json()
        return {"items": body, "headers": dict(response.headers)}

    async def _paged(
        self, path: str, *, params: dict[str, Any] | None = None, limit: int, row: Any
    ) -> Results[Any]:
        async def request(asked: dict[str, Any]) -> Any:
            return await self._envelope(
                "GET", path, params={**(params or {}), **asked}
            )

        return await page_through(
            request,
            style=_GITHUB_PAGING,
            limit=limit,
            page_size=GITHUB_PAGE_CAP,
            row=row,
        )

    # -- identity and repos -------------------------------------------------

    async def whoami(self) -> GitHubUser:
        return GitHubUser.from_api(await self._request("GET", "/user"))

    async def find_users(self, query: str, *, limit: int = 20) -> list[GitHubUser]:
        """Search for people by name, login, or email.

        The join between what someone is called and the ``login`` every
        assignment takes.
        """
        body = await self._request(
            "GET",
            "/search/users",
            params={"q": query, "per_page": min(limit, GITHUB_PAGE_CAP)},
        )
        return [GitHubUser.from_api(u) for u in (body or {}).get("items", [])]

    async def list_repos(
        self, owner: str = "", *, limit: int = 50, sort: str = "updated"
    ) -> Results[GitHubRepo]:
        """Repositories for an owner, or for the authenticated user."""
        path = f"/users/{owner}/repos" if owner else "/user/repos"
        return await self._paged(
            path, params={"sort": sort}, limit=limit, row=GitHubRepo.from_api
        )

    async def get_repo(self, repo: str) -> GitHubRepo:
        return GitHubRepo.from_api(await self._request("GET", f"/repos/{repo}"))

    # -- issues -------------------------------------------------------------

    async def list_issues(
        self,
        repo: str,
        *,
        state: str = "open",
        labels: str = "",
        assignee: str = "",
        since: str = "",
        limit: int = 50,
        include_pull_requests: bool = False,
    ) -> Results[GitHubIssue]:
        """Issues in a repository, pull requests excluded by default.

        The exclusion is the point. GitHub returns PRs from this endpoint by
        design, so an unfiltered listing answers "how many open issues" with a
        number that includes pull requests — wrong, and with nothing to notice.
        """
        rows = await self._paged(
            f"/repos/{repo}/issues",
            params={
                "state": state,
                "labels": labels or None,
                "assignee": assignee or None,
                "since": since or None,
            },
            limit=limit,
            row=GitHubIssue.from_api,
        )
        if include_pull_requests:
            return rows
        # Rebuilt rather than comprehended: a plain list would discard the
        # coverage flags, which is the exact loss `Results` exists to prevent.
        # `total` is deliberately dropped — it counted the PRs too, so keeping
        # it would report a total larger than what is being returned.
        return Results(
            [issue for issue in rows if not issue.is_pull_request],
            complete=rows.complete,
            cursor=rows.cursor,
        )

    async def get_issue(self, repo: str, number: int) -> GitHubIssue:
        return GitHubIssue.from_api(
            await self._request("GET", f"/repos/{repo}/issues/{number}")
        )

    async def create_issue(
        self,
        repo: str,
        title: str,
        *,
        body: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> GitHubIssue:
        payload: dict[str, Any] = {"title": title}
        if body:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        return GitHubIssue.from_api(
            await self._request("POST", f"/repos/{repo}/issues", json=payload)
        )

    async def update_issue(
        self,
        repo: str,
        number: int,
        *,
        title: str = "",
        body: str = "",
        state: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> GitHubIssue:
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if body:
            payload["body"] = body
        if state:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        if assignees is not None:
            payload["assignees"] = assignees
        return GitHubIssue.from_api(
            await self._request("PATCH", f"/repos/{repo}/issues/{number}", json=payload)
        )

    async def list_comments(
        self, repo: str, number: int, *, limit: int = 50
    ) -> Results[GitHubComment]:
        return await self._paged(
            f"/repos/{repo}/issues/{number}/comments",
            limit=limit,
            row=GitHubComment.from_api,
        )

    async def add_comment(self, repo: str, number: int, body: str) -> GitHubComment:
        return GitHubComment.from_api(
            await self._request(
                "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
            )
        )

    # -- pull requests ------------------------------------------------------

    async def list_pull_requests(
        self,
        repo: str,
        *,
        state: str = "open",
        base: str = "",
        limit: int = 50,
    ) -> Results[GitHubPullRequest]:
        return await self._paged(
            f"/repos/{repo}/pulls",
            params={"state": state, "base": base or None},
            limit=limit,
            row=GitHubPullRequest.from_api,
        )

    async def get_pull_request(self, repo: str, number: int) -> GitHubPullRequest:
        """One pull request, from the pull request endpoint.

        Deliberately not reachable from an issues listing: an id taken from
        there is an *issue* id and addresses something else.
        """
        return GitHubPullRequest.from_api(
            await self._request("GET", f"/repos/{repo}/pulls/{number}")
        )

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        *,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
    ) -> GitHubPullRequest:
        return GitHubPullRequest.from_api(
            await self._request(
                "POST",
                f"/repos/{repo}/pulls",
                json={
                    "title": title,
                    "head": head,
                    "base": base,
                    "body": body,
                    "draft": draft,
                },
            )
        )

    # -- search -------------------------------------------------------------

    async def search_issues(self, query: str, *, limit: int = 30) -> Results[GitHubIssue]:
        """Search issues and pull requests across GitHub.

        Reports what is *retrievable*, not ``total_count``: search will not
        page past 1,000 results however many exist, so echoing the total would
        overstate coverage by any margin.
        """
        return await self._search("/search/issues", query, limit, GitHubIssue.from_api)

    async def search_repos(self, query: str, *, limit: int = 30) -> Results[GitHubRepo]:
        return await self._search(
            "/search/repositories", query, limit, GitHubRepo.from_api
        )

    async def _search(
        self, path: str, query: str, limit: int, row: Any
    ) -> Results[Any]:
        capped = min(limit, SEARCH_TOTAL_CAP)
        timed_out = False

        async def request(asked: dict[str, Any]) -> Any:
            nonlocal timed_out
            envelope = await self._envelope(
                "GET", path, params={"q": query, **asked}
            )
            body = envelope["items"] or {}
            if body.get("incomplete_results"):
                timed_out = True
            # The search envelope nests its rows, so they are lifted to where
            # the paging style expects them.
            return {"items": body.get("items") or [], "headers": envelope["headers"]}

        found = await page_through(
            request,
            style=_GITHUB_PAGING,
            limit=capped,
            page_size=min(capped, GITHUB_PAGE_CAP),
            row=row,
        )
        if timed_out:
            # GitHub's own words: more results might have been found, or might
            # not. Either way this is not the whole answer.
            return Results(list(found), complete=False, cursor=found.cursor)
        return found


#: GitHub signals the next page in a Link header; no `rel="next"` is the end.
_GITHUB_PAGING = HeaderPaging(
    items="items",
    page_param="page",
    size_param="per_page",
    link_header="link",
)


def _clean(params: Any) -> Any:
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> GitHubError:
    """Turn a failed response into the narrowest error that fits.

    The 403 split is the one to get right: GitHub uses the same status for
    "you are rate limited, wait" and "your token lacks the scope, never". The
    remaining-quota header is what separates them.
    """
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = body.get("message") if isinstance(body, dict) else None
    message = f"GitHub {status}: {detail or response.text[:200] or 'request failed'}"
    headers = {str(k).lower(): v for k, v in dict(response.headers).items()}
    retry_after = float(headers.get("retry-after", 0) or 0)

    if status == 429:
        return GitHubRateLimited(message, status=status, retry_after=retry_after)
    if status == 403 and str(headers.get("x-ratelimit-remaining", "")) == "0":
        return GitHubRateLimited(message, status=status, retry_after=retry_after)
    if status == 401:
        return GitHubAuthError(message, status=status)
    if status == 404:
        return GitHubNotFound(
            f"{message} — GitHub returns 404 for a resource that exists but "
            "this token cannot see, so check the token's scopes and repository "
            "access as well as the path.",
            status=status,
        )
    if 400 <= status < 500:
        return GitHubPermanentError(message, status=status)
    return GitHubError(message, status=status)


_default_client: GitHubClient | None = None


def get_default_client() -> GitHubClient:
    """Return (or create) the module-level client from the environment."""
    global _default_client
    if _default_client is None:
        _default_client = GitHubClient()
    return _default_client
