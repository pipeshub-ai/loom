"""Async Asana API client — pure httpx, no vendor SDK.

Credentials resolve from an explicit argument, then the environment:

    ASANA_ACCESS_TOKEN    a personal access token, or an OAuth access token

Both are bearer tokens, so unlike ClickUp there is only one shape to get right.

Two things about this API drive the design here:

**Everything is wrapped.** Responses are ``{"data": …}`` and errors are
``{"errors": [{"message": …}]}``. The unwrapping happens once, in ``_request``.

**Only some endpoints page.** Listing tasks in a project pages with an opaque
``offset`` token; *search* does not page at all and is premium-only. That split
is reflected in the return types rather than smoothed over — a search returns a
plain list, so nothing claims a coverage guarantee the endpoint cannot make.

Asana also returns only the fields ``opt_fields`` asks for, so the field list is
declared per call. Omitting it returns bare gids and names, which reads like the
data is missing rather than like it was never requested.
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.asana.models import (
    AsanaProject,
    AsanaSection,
    AsanaStory,
    AsanaTask,
    AsanaUser,
    AsanaWorkspace,
)
from loom.toolsets.pagination import CursorPaging, Results, page_through

BASE_URL = "https://app.asana.com/api/1.0"

#: Asana's hard ceiling for a page, whatever ``limit`` asks for.
ASANA_PAGE_CAP = 100

#: What a task is worth fetching. Asana returns *only* what is asked for, and
#: the default response is a gid and a name — which looks like a task with no
#: assignee and no due date rather than like an under-specified request.
TASK_FIELDS = (
    "name,notes,completed,completed_at,created_at,modified_at,due_on,due_at,"
    "permalink_url,assignee.name,assignee.gid,projects.name,tags.name,parent.gid"
)
PROJECT_FIELDS = "name,archived,notes,permalink_url,owner.name"
STORY_FIELDS = "text,type,resource_subtype,created_at,created_by.name"


class AsanaError(WorkflowError):
    """An Asana request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0, **kw: Any) -> None:
        super().__init__(message)
        self.status = status


class AsanaPermanentError(AsanaError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class AsanaAuthError(AsanaPermanentError):
    """Missing, malformed, expired, or revoked credentials."""


class AsanaPremiumRequired(AsanaPermanentError):  # noqa: N818 - names a state
    """The endpoint exists but this workspace's plan does not include it.

    Its own class because it is the one Asana failure a caller can act on
    without changing any code — search is premium-only, and a workflow that
    hits this should fall back to listing a project rather than retrying.
    """


class AsanaRateLimited(AsanaError):  # noqa: N818 - names a state
    """Too many requests. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class AsanaClient:
    """Thin async wrapper around the Asana API.

    Parameters
    ----------
    access_token:
        A personal access token or an OAuth access token. Falls back to
        ``ASANA_ACCESS_TOKEN``.
    """

    def __init__(
        self,
        access_token: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        if not self._token:
            raise AsanaAuthError(
                "Asana needs a token: set ASANA_ACCESS_TOKEN (a personal "
                "access token) or pass access_token=."
            )

    # -- transport ----------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                params=_clean(params),
                json=json,
            )
        if response.status_code >= 400:
            raise _classify(response)
        if not response.content:
            return {}
        return response.json()

    async def _data(self, method: str, path: str, **kw: Any) -> Any:
        """A request whose payload is the ``data`` envelope, unwrapped."""
        body = await self._request(method, path, **kw)
        return body.get("data") if isinstance(body, dict) else body

    # -- identity and structure --------------------------------------------

    async def whoami(self) -> AsanaUser:
        return AsanaUser.from_api(await self._data("GET", "/users/me"))

    async def list_workspaces(self) -> list[AsanaWorkspace]:
        rows = await self._data("GET", "/workspaces") or []
        return [AsanaWorkspace.from_api(w) for w in rows]

    async def list_projects(
        self, workspace_gid: str, *, limit: int = 50, archived: bool = False
    ) -> Results[AsanaProject]:
        async def request(params: dict[str, Any]) -> Any:
            return await self._request(
                "GET",
                "/projects",
                params={
                    **params,
                    "workspace": workspace_gid,
                    "archived": archived,
                    "opt_fields": PROJECT_FIELDS,
                },
            )

        return await page_through(
            request,
            style=_ASANA_PAGING,
            limit=limit,
            page_size=ASANA_PAGE_CAP,
            row=AsanaProject.from_api,
        )

    async def list_sections(self, project_gid: str) -> list[AsanaSection]:
        rows = await self._data("GET", f"/projects/{project_gid}/sections") or []
        return [AsanaSection.from_api(s) for s in rows]

    # -- tasks --------------------------------------------------------------

    async def list_tasks(
        self,
        project_gid: str,
        *,
        limit: int = 50,
        completed_since: str = "",
    ) -> Results[AsanaTask]:
        async def request(params: dict[str, Any]) -> Any:
            return await self._request(
                "GET",
                "/tasks",
                params={
                    **params,
                    "project": project_gid,
                    "completed_since": completed_since or None,
                    "opt_fields": TASK_FIELDS,
                },
            )

        return await page_through(
            request,
            style=_ASANA_PAGING,
            limit=limit,
            page_size=ASANA_PAGE_CAP,
            row=AsanaTask.from_api,
        )

    async def search_tasks(
        self,
        workspace_gid: str,
        *,
        text: str = "",
        assignee_gid: str = "",
        project_gids: list[str] | None = None,
        completed: bool | None = None,
        limit: int = 50,
        sort_by: str = "modified_at",
    ) -> list[AsanaTask]:
        """Full-text search across a workspace.

        Returns a plain list, not ``Results``, and that is a statement about
        the endpoint rather than an oversight: Asana's search **does not
        support offset pagination** — its own documentation says results are
        unstable across identical queries — so there is no page to follow and
        no coverage to report. Declaring ``Results`` here would promise a
        completeness guarantee the API cannot keep.

        Premium-only. A workspace on a free plan raises
        :class:`AsanaPremiumRequired`, which is worth catching to fall back to
        :meth:`list_tasks` on a known project.
        """
        params: dict[str, Any] = {
            "limit": min(limit, ASANA_PAGE_CAP),
            "sort_by": sort_by,
            "opt_fields": TASK_FIELDS,
        }
        if text:
            params["text"] = text
        if assignee_gid:
            params["assignee.any"] = assignee_gid
        if project_gids:
            params["projects.any"] = ",".join(project_gids)
        if completed is not None:
            params["completed"] = completed

        rows = await self._data(
            "GET", f"/workspaces/{workspace_gid}/tasks/search", params=params
        )
        return [AsanaTask.from_api(t) for t in (rows or [])]

    async def get_task(self, task_gid: str) -> AsanaTask:
        return AsanaTask.from_api(
            await self._data(
                "GET", f"/tasks/{task_gid}", params={"opt_fields": TASK_FIELDS}
            )
        )

    async def create_task(
        self,
        workspace_gid: str = "",
        *,
        name: str,
        notes: str = "",
        projects: list[str] | None = None,
        assignee_gid: str = "",
        due_on: str = "",
        parent: str = "",
    ) -> AsanaTask:
        payload: dict[str, Any] = {"name": name}
        if notes:
            payload["notes"] = notes
        if projects:
            payload["projects"] = projects
        if assignee_gid:
            payload["assignee"] = assignee_gid
        if due_on:
            payload["due_on"] = due_on
        if parent:
            payload["parent"] = parent
        elif workspace_gid and not projects:
            # Exactly one home. Asana's words: "The workspace need not be set
            # explicitly if you specify projects or a parent task instead" —
            # and sending a workspace *alongside* projects is at best
            # redundant and at worst a conflict when the project lives in a
            # different workspace. Sending none at all is a 400 that names no
            # field, which is why the fallback exists.
            payload["workspace"] = workspace_gid

        return AsanaTask.from_api(
            await self._data(
                "POST",
                "/tasks",
                params={"opt_fields": TASK_FIELDS},
                json={"data": payload},
            )
        )

    async def update_task(
        self,
        task_gid: str,
        *,
        name: str = "",
        notes: str = "",
        completed: bool | None = None,
        assignee_gid: str = "",
        due_on: str = "",
    ) -> AsanaTask:
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if notes:
            payload["notes"] = notes
        if completed is not None:
            payload["completed"] = completed
        if assignee_gid:
            payload["assignee"] = assignee_gid
        if due_on:
            payload["due_on"] = due_on

        return AsanaTask.from_api(
            await self._data(
                "PUT",
                f"/tasks/{task_gid}",
                params={"opt_fields": TASK_FIELDS},
                json={"data": payload},
            )
        )

    async def delete_task(self, task_gid: str) -> None:
        await self._request("DELETE", f"/tasks/{task_gid}")

    # -- comments -----------------------------------------------------------

    async def list_comments(self, task_gid: str, *, limit: int = 50) -> list[AsanaStory]:
        """Comments on a task.

        Filtered from the story feed, which also carries every field change
        Asana has recorded. "Show me the comments" almost never means "show me
        that someone edited the due date in March".
        """
        body = await self._request(
            "GET",
            f"/tasks/{task_gid}/stories",
            params={"limit": min(limit, ASANA_PAGE_CAP), "opt_fields": STORY_FIELDS},
        )
        stories = [AsanaStory.from_api(s) for s in (body.get("data") or [])]
        return [s for s in stories if s.type == "comment"]

    async def add_comment(self, task_gid: str, text: str) -> AsanaStory:
        return AsanaStory.from_api(
            await self._data(
                "POST",
                f"/tasks/{task_gid}/stories",
                params={"opt_fields": STORY_FIELDS},
                json={"data": {"text": text}},
            )
        )

    # -- people -------------------------------------------------------------

    async def find_users(
        self, workspace_gid: str, query: str = "", *, count: int = 20
    ) -> list[AsanaUser]:
        """Find people by name, via typeahead.

        The join between a person's name and the gid every assignment needs.
        Asana ranks these most-contacted first, which is usually what someone
        means by "assign it to Priya".
        """
        rows = await self._data(
            "GET",
            f"/workspaces/{workspace_gid}/typeahead",
            params={
                "resource_type": "user",
                "query": query or None,
                "count": min(count, ASANA_PAGE_CAP),
                "opt_fields": "name,email",
            },
        )
        return [AsanaUser.from_api(u) for u in (rows or [])]


#: Asana hands back a whole next-page URI with the offset inside it, so the
#: cursor is parsed back out of the query string rather than read as a field.
_ASANA_PAGING = CursorPaging(
    items="data",
    size_param="limit",
    param="offset",
    link=("next_page", "uri"),
)


def _clean(params: Any) -> Any:
    """Drop unset filters — httpx would otherwise send the string ``"None"``."""
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> AsanaError:
    """Turn a failed response into the narrowest error that fits."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    detail = (errors or [{}])[0].get("message") if errors else (response.text or "")
    message = f"Asana {status}: {detail or 'request failed'}"

    if status == 429:
        return AsanaRateLimited(
            message,
            status=status,
            retry_after=float(response.headers.get("Retry-After", 0) or 0),
        )
    if status in (401, 403):
        # 402 is the documented "payment required", but Asana also reports a
        # premium-only endpoint as 403 with the reason in the message — so the
        # text is what separates "you may not" from "your plan may not".
        if "premium" in (detail or "").lower() or status == 402:
            return AsanaPremiumRequired(message, status=status)
        return AsanaAuthError(message, status=status)
    if status == 402:
        return AsanaPremiumRequired(message, status=status)
    if 400 <= status < 500:
        return AsanaPermanentError(message, status=status)
    return AsanaError(message, status=status)


