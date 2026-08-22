"""Async ClickUp API v2 client — pure httpx, no vendor SDK.

Credentials resolve from an explicit argument, then the environment:

    CLICKUP_API_TOKEN     a personal token, ``pk_…``
    CLICKUP_OAUTH_TOKEN   an OAuth 2.0 access token

**The two are sent differently**, which is the one thing about ClickUp auth that
catches people out: a personal token goes in ``Authorization`` *raw*, with no
scheme, while an OAuth token takes the usual ``Bearer`` prefix. Sending a
personal token as ``Bearer pk_…`` returns 401 with no explanation of why.

Paging is ordinal — ``page=0``, ``page=1`` — with a ``last_page`` flag, which is
:class:`~loom.toolsets.pagination.PageNumberPaging` rather than the row-offset
style Jira uses. ClickUp caps a page at 100 whatever you ask for.
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.clickup.models import (
    ClickUpComment,
    ClickUpContainer,
    ClickUpTask,
    ClickUpUser,
    ClickUpWorkspace,
)
from loom.toolsets.pagination import (
    PageNumberPaging,
    Results,
    page_through,
)

#: ClickUp caps a page here whatever the request asks for, and says nothing
#: when it does.
CLICKUP_PAGE_CAP = 100

BASE_URL = "https://api.clickup.com/api/v2"


class ClickUpError(WorkflowError):
    """A ClickUp request failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self, message: str, *, status: int = 0, code: str = "", **kw: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        """ClickUp's own ``ECODE``, which identifies the failure far better than
        the status does — ``OAUTH_027`` is a missing scope, not a bad request."""


class ClickUpPermanentError(ClickUpError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    A bad list id, a task that does not exist, a token without the scope. The
    two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class ClickUpAuthError(ClickUpPermanentError):
    """Missing, malformed, or revoked credentials."""


class ClickUpRateLimited(ClickUpError):  # noqa: N818 - names a state
    """Quota exceeded. Retryable, and the caller should back off.

    ClickUp allows 100 requests a minute per token on most plans, which a
    workflow paging through a large list reaches easily.
    """

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ClickUpClient:
    """Thin async wrapper around the ClickUp API v2.

    Parameters
    ----------
    api_token:
        A personal token (``pk_…``). Falls back to ``CLICKUP_API_TOKEN``.
    oauth_token:
        An OAuth 2.0 access token. Falls back to ``CLICKUP_OAUTH_TOKEN``.
        Takes precedence when both are present, because an app that went to the
        trouble of an OAuth flow meant to act as that user.
    """

    def __init__(
        self,
        api_token: str | None = None,
        oauth_token: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._token = api_token
        self._oauth = oauth_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        if not self._token and not self._oauth:
            # At construction, not at first request: the message names the fix
            # and appears where the object was built, rather than five frames
            # into a workflow step.
            raise ClickUpAuthError(
                "ClickUp needs a token: set CLICKUP_API_TOKEN (a personal "
                "'pk_…' token) or CLICKUP_OAUTH_TOKEN, or pass api_token=."
            )

    # -- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = self._oauth or self._token
        scheme = "Bearer " if self._oauth else ""
        return {
            "Authorization": f"{scheme}{token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers(),
                params=_clean(params),
                json=json,
            )
        if response.status_code >= 400:
            raise _classify(response)
        if not response.content:
            return {}
        return response.json()

    # -- workspace navigation ----------------------------------------------

    async def list_workspaces(self) -> list[ClickUpWorkspace]:
        body = await self._request("GET", "/team")
        return [ClickUpWorkspace.from_api(t) for t in body.get("teams", [])]

    async def list_spaces(
        self, workspace_id: str, *, archived: bool = False
    ) -> list[ClickUpContainer]:
        body = await self._request(
            "GET", f"/team/{workspace_id}/space", params={"archived": archived}
        )
        return [
            ClickUpContainer.from_api(s, "space") for s in body.get("spaces", [])
        ]

    async def list_folders(
        self, space_id: str, *, archived: bool = False
    ) -> list[ClickUpContainer]:
        body = await self._request(
            "GET", f"/space/{space_id}/folder", params={"archived": archived}
        )
        return [
            ClickUpContainer.from_api(f, "folder") for f in body.get("folders", [])
        ]

    async def list_lists(
        self,
        space_id: str = "",
        folder_id: str = "",
        *,
        archived: bool = False,
    ) -> list[ClickUpContainer]:
        """Lists in a folder, or the folderless lists directly in a space.

        ClickUp models these as two endpoints because a list may sit either
        inside a folder or straight in a space, and a caller holding a space id
        cannot see the folderless ones through the folder route.
        """
        if not space_id and not folder_id:
            raise ClickUpPermanentError(
                "list_lists needs either space_id or folder_id"
            )
        path = (
            f"/folder/{folder_id}/list" if folder_id else f"/space/{space_id}/list"
        )
        body = await self._request("GET", path, params={"archived": archived})
        return [ClickUpContainer.from_api(item, "list") for item in body.get("lists", [])]

    # -- tasks --------------------------------------------------------------

    async def list_tasks(
        self,
        list_id: str,
        *,
        limit: int = 50,
        include_closed: bool = False,
        subtasks: bool = False,
        statuses: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> Results[ClickUpTask]:
        async def request(params: dict[str, Any]) -> Any:
            return await self._request(
                "GET",
                f"/list/{list_id}/task",
                params={
                    **params,
                    "include_closed": include_closed,
                    "subtasks": subtasks,
                    "statuses[]": statuses or None,
                    "assignees[]": assignees or None,
                },
            )

        return await page_through(
            request,
            style=PageNumberPaging(items="tasks"),
            limit=limit,
            page_size=CLICKUP_PAGE_CAP,
            row=ClickUpTask.from_api,
        )

    async def search_tasks(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        space_ids: list[str] | None = None,
        list_ids: list[str] | None = None,
        statuses: list[str] | None = None,
        assignees: list[str] | None = None,
        include_closed: bool = False,
    ) -> Results[ClickUpTask]:
        """Tasks across a whole workspace, filtered.

        The workspace-wide view, where ``list_tasks`` is scoped to one list.
        ClickUp calls the filters "team tasks"; the ``assignees`` filter takes
        numeric user ids, not names — see :meth:`find_members`.
        """

        async def request(params: dict[str, Any]) -> Any:
            return await self._request(
                "GET",
                f"/team/{workspace_id}/task",
                params={
                    **params,
                    "include_closed": include_closed,
                    "space_ids[]": space_ids or None,
                    "list_ids[]": list_ids or None,
                    "statuses[]": statuses or None,
                    "assignees[]": assignees or None,
                },
            )

        return await page_through(
            request,
            style=PageNumberPaging(items="tasks"),
            limit=limit,
            page_size=CLICKUP_PAGE_CAP,
            row=ClickUpTask.from_api,
        )

    async def get_task(self, task_id: str) -> ClickUpTask:
        return ClickUpTask.from_api(await self._request("GET", f"/task/{task_id}"))

    async def create_task(
        self,
        list_id: str,
        name: str,
        *,
        description: str = "",
        assignees: list[str] | None = None,
        status: str = "",
        priority: int | None = None,
        due_date: int | None = None,
        tags: list[str] | None = None,
        parent: str = "",
    ) -> ClickUpTask:
        payload: dict[str, Any] = {"name": name}
        if description:
            payload["description"] = description
        if assignees:
            # Numeric ids, and ClickUp rejects them as strings.
            payload["assignees"] = [int(a) for a in assignees if str(a).isdigit()]
        if status:
            payload["status"] = status
        if priority is not None:
            payload["priority"] = priority
        if due_date is not None:
            payload["due_date"] = due_date
        if tags:
            payload["tags"] = tags
        if parent:
            payload["parent"] = parent
        return ClickUpTask.from_api(
            await self._request("POST", f"/list/{list_id}/task", json=payload)
        )

    async def update_task(
        self,
        task_id: str,
        *,
        name: str = "",
        description: str = "",
        status: str = "",
        priority: int | None = None,
        due_date: int | None = None,
        archived: bool | None = None,
    ) -> ClickUpTask:
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if status:
            payload["status"] = status
        if priority is not None:
            payload["priority"] = priority
        if due_date is not None:
            payload["due_date"] = due_date
        if archived is not None:
            payload["archived"] = archived
        return ClickUpTask.from_api(
            await self._request("PUT", f"/task/{task_id}", json=payload)
        )

    async def delete_task(self, task_id: str) -> None:
        await self._request("DELETE", f"/task/{task_id}")

    # -- comments -----------------------------------------------------------

    async def list_comments(self, task_id: str) -> list[ClickUpComment]:
        body = await self._request("GET", f"/task/{task_id}/comment")
        return [ClickUpComment.from_api(c) for c in body.get("comments", [])]

    async def create_comment(
        self, task_id: str, text: str, *, assignee: str = "", notify_all: bool = False
    ) -> ClickUpComment:
        payload: dict[str, Any] = {
            "comment_text": text,
            "notify_all": notify_all,
        }
        if assignee and str(assignee).isdigit():
            payload["assignee"] = int(assignee)
        body = await self._request(
            "POST", f"/task/{task_id}/comment", json=payload
        )
        # The create response carries an id and not much else, so the text is
        # echoed from the request rather than reported as empty.
        return ClickUpComment(id=str(body.get("id", "")), text=text)

    # -- people -------------------------------------------------------------

    async def find_members(self, workspace_id: str, query: str = "") -> list[ClickUpUser]:
        """Workspace members, optionally narrowed by name or email.

        The join between "what a person is called" and the numeric id every
        write wants. ClickUp has no member-search endpoint, so the filtering is
        done here over the workspace roster — which is also why it is honest
        about being a substring match rather than a ranked search.
        """
        body = await self._request("GET", "/team")
        people: list[ClickUpUser] = []
        for team in body.get("teams", []):
            if str(team.get("id", "")) != str(workspace_id):
                continue
            for member in team.get("members", []):
                user = ClickUpUser.from_api(member.get("user") or member)
                if not query or query.lower() in (
                    f"{user.username} {user.email}".lower()
                ):
                    people.append(user)
        return people

    async def whoami(self) -> ClickUpUser:
        body = await self._request("GET", "/user")
        return ClickUpUser.from_api(body.get("user"))


def _clean(params: Any) -> Any:
    """Drop unset filters.

    httpx encodes ``None`` as the string ``"None"``, and ClickUp treats an
    unknown status filter as "match nothing" rather than as an error — so an
    omitted argument would silently return zero tasks.
    """
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> ClickUpError:
    """Turn a failed response into the narrowest error that fits.

    A 4xx is not transient, and retrying one spends three attempts to reach the
    same answer. Only 429 and 5xx stay retryable.
    """
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = body.get("err") or response.text or f"HTTP {status}"
    code = str(body.get("ECODE", "") or "")
    message = f"ClickUp {status}: {detail}" + (f" ({code})" if code else "")

    if status == 429:
        return ClickUpRateLimited(
            message,
            status=status,
            code=code,
            retry_after=float(response.headers.get("X-RateLimit-Reset", 0) or 0),
        )
    if status in (401, 403):
        return ClickUpAuthError(message, status=status, code=code)
    if 400 <= status < 500:
        return ClickUpPermanentError(message, status=status, code=code)
    return ClickUpError(message, status=status, code=code)


