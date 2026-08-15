"""Lazy httpx-based Jira REST API v3 client.

No ``jira`` pip package required — pure httpx over the REST API.
Credentials are read from environment variables at first use:

    JIRA_URL        https://yourorg.atlassian.net
    JIRA_EMAIL      your@email.com
    JIRA_API_TOKEN  your-api-token

All public methods return typed Pydantic models from ``models.py``.
A future iteration may auto-generate this client from the official
Jira Cloud OpenAPI 3.1 spec at
https://developer.atlassian.com/cloud/jira/platform/rest/v3/
"""

from __future__ import annotations

import base64
import os
from typing import Any

from loom.connectors.credentials import current_credential_store, resolve_bearer_token
from loom.toolsets.jira.models import (
    Comment,
    CreatedIssue,
    JiraIssue,
    JiraProject,
    JiraProjectDetail,
    JiraUser,
    ProjectMetadata,
    Transition,
    UserLookup,
)
from loom.toolsets.pagination import (
    OffsetPaging,
    Results,
    TokenPaging,
    page_through,
)

#: Jira Cloud caps a page here whatever ``maxResults`` asks for, and reports
#: no error when it does.
JIRA_PAGE_CAP = 100


class JiraClient:
    """Thin async wrapper around the Jira REST API v3.

    Parameters
    ----------
    base_url:
        Base URL of your Jira instance, e.g. ``https://myorg.atlassian.net``.
        Falls back to ``JIRA_URL`` env var.
    email:
        Atlassian account email. Falls back to ``JIRA_EMAIL``.
    api_token:
        Atlassian API token. Falls back to ``JIRA_API_TOKEN``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        *,
        credential_name: str = "jira",
    ) -> None:
        self._base_url = (base_url or os.environ.get("JIRA_URL", "")).rstrip("/")
        self._email = email or os.environ.get("JIRA_EMAIL", "")
        self._token = api_token or os.environ.get("JIRA_API_TOKEN", "")
        self._credential_name = credential_name

        if not self._base_url:
            msg = "JIRA_URL is required (env var or base_url argument)"
            raise ValueError(msg)
        # A CredentialStore bound to the current run might supply what the
        # environment did not, but that can only be checked with an await —
        # deferred to _headers() at first actual call. Raise now only when
        # nothing could possibly save it: no email/token *and* no store
        # bound at all, exactly today's behaviour when neither is true.
        if (not self._email or not self._token) and current_credential_store() is None:
            msg = "JIRA_EMAIL and JIRA_API_TOKEN are required"
            raise ValueError(msg)

    async def _headers(self) -> dict[str, str]:
        """Basic auth from email/token, or a CredentialStore-issued bearer token.

        The store is checked first and on every call — never cached — so a
        run that connected 'jira' after this client was constructed, or
        whose stored token the store has since refreshed, is never stuck
        with what was true when ``__init__`` ran. Falls back to Basic auth
        when no store is bound or it has nothing under ``credential_name``,
        which is exactly today's only behaviour.
        """
        token = await resolve_bearer_token(self._credential_name)
        if token:
            return {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        if self._email and self._token:
            credentials = base64.b64encode(f"{self._email}:{self._token}".encode()).decode()
            return {
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        raise ValueError(
            "JIRA_EMAIL and JIRA_API_TOKEN are required, or connect a "
            f"'{self._credential_name}' credential via a CredentialStore"
        )

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> Any:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=json)
            resp.raise_for_status()
            return resp.json()

    async def _put(self, path: str, json: dict[str, Any]) -> Any:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(url, headers=headers, json=json)
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    async def _delete(self, path: str, **params: Any) -> None:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(url, headers=headers, params=params)
            resp.raise_for_status()

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def search_issues(
        self,
        jql: str,
        max_results: int = 20,
        fields: list[str] | None = None,
    ) -> Results:
        """Search using JQL and return typed issues, following every page.

        Uses POST /rest/api/3/search/jql (Jira Cloud current endpoint), which
        caps a page at 100 however large a ``maxResults`` you send — and says
        nothing when it does. Asking for 500 and receiving 100 with a 200 OK is
        the failure this loop exists to prevent.

        The result is a list, so existing callers are unaffected; check
        ``.complete`` to find out whether ``max_results`` cut it off.
        """
        default_fields = [
            "summary", "status", "assignee", "priority",
            "issuetype", "created", "updated", "description",
            "comment", "labels", "project",
        ]

        return await page_through(
            lambda params: self._post(
                "search/jql",
                {"jql": jql, "fields": fields or default_fields, **params},
            ),
            style=TokenPaging(
                items="issues",
                token_param="nextPageToken",
                last_field="isLast",
                total_field="total",
            ),
            limit=max_results,
            page_size=JIRA_PAGE_CAP,
            row=_flatten_issue,
        )

    async def get_issue(self, issue_key: str) -> JiraIssue:
        """Fetch a single issue by key, e.g. ``'PROJ-123'``."""
        data = await self._get(f"issue/{issue_key}")
        return _flatten_issue(data)

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Story",
        priority: str = "Medium",
        labels: list[str] | None = None,
        assignee_account_id: str | None = None,
    ) -> CreatedIssue:
        """Create a new issue. Returns key, id, and browse URL."""
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "priority": {"name": priority},
        }
        if description:
            fields["description"] = {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": description}
                ]}],
            }
        if labels:
            fields["labels"] = labels
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}

        data = await self._post("issue", {"fields": fields})
        key = data["key"]
        return CreatedIssue(
            key=key,
            id=data["id"],
            url=f"{self._base_url}/browse/{key}",
        )

    async def update_issue(
        self, issue_key: str, fields: dict[str, Any]
    ) -> JiraIssue:
        """Update arbitrary fields on an issue. Returns updated issue."""
        await self._put(f"issue/{issue_key}", {"fields": fields})
        return await self.get_issue(issue_key)

    async def add_comment(
        self, issue_key: str, comment: str
    ) -> Comment:
        """Add a plain-text comment to an issue."""
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": comment}
                ]}],
            }
        }
        data = await self._post(f"issue/{issue_key}/comment", body)
        return Comment(
            id=data["id"],
            author=data.get("author", {}).get("displayName", ""),
            created=data.get("created", ""),
        )

    async def get_transitions(
        self, issue_key: str
    ) -> list[Transition]:
        """List available status transitions for an issue."""
        data = await self._get(f"issue/{issue_key}/transitions")
        return [
            Transition(id=t["id"], name=t["name"])
            for t in data.get("transitions", [])
        ]

    async def transition_issue(
        self, issue_key: str, transition_id: str
    ) -> JiraIssue:
        """Move an issue to a new status using a transition id."""
        await self._post(
            f"issue/{issue_key}/transitions",
            {"transition": {"id": transition_id}},
        )
        return await self.get_issue(issue_key)

    async def assign_issue(
        self, issue_key: str, account_id: str | None
    ) -> JiraIssue:
        """Assign an issue. Pass account_id=None to unassign."""
        await self._put(
            f"issue/{issue_key}/assignee",
            {"accountId": account_id},
        )
        return await self.get_issue(issue_key)

    async def delete_issue(self, issue_key: str, delete_subtasks: bool = False) -> str:
        """Permanently delete an issue. Returns the key that was deleted."""
        await self._delete(
            f"issue/{issue_key}",
            deleteSubtasks="true" if delete_subtasks else "false",
        )
        return issue_key

    async def get_comments(self, issue_key: str, max_results: int = 20) -> Results:
        """Read an issue's comments, flattened out of Atlassian Document Format.

        Offset-paged rather than token-paged — the same endpoint family, a
        different dialect, which is why the loop takes the paging scheme as an
        argument instead of assuming one.
        """

        return await page_through(
            lambda params: self._get(f"issue/{issue_key}/comment", **params),
            style=OffsetPaging(items="comments", total_field="total"),
            limit=max_results,
            page_size=JIRA_PAGE_CAP,
            row=lambda item: Comment(
                id=item.get("id", ""),
                author=(item.get("author") or {}).get("displayName", ""),
                created=item.get("created", ""),
                body=_flatten_adf(item.get("body")),
            ),
        )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def list_projects(self) -> list[JiraProject]:
        """List all accessible projects."""
        data = await self._get("project")
        return [
            JiraProject(key=p["key"], name=p["name"], id=p["id"])
            for p in data
        ]

    async def get_project(self, project_key: str) -> JiraProjectDetail:
        """Get project details by key."""
        data = await self._get(f"project/{project_key}")
        return JiraProjectDetail(
            key=data["key"],
            name=data["name"],
            id=data["id"],
            description=data.get("description", ""),
            lead=data.get("lead", {}).get("displayName", ""),
        )

    async def get_metadata(self, project_key: str) -> ProjectMetadata:
        """The status, priority, and issue-type names this project actually uses.

        Worth a call before filtering. These are per-project configuration, and
        a JQL filter naming a status the board does not have returns zero rows
        without an error — indistinguishable from "there is no such work".
        """
        statuses = await self._get(f"project/{project_key}/statuses")
        priorities = await self._get("priority")
        return ProjectMetadata(
            project_key=project_key,
            statuses=sorted(
                {s.get("name", "") for t in statuses for s in t.get("statuses", [])}
            ),
            priorities=[p.get("name", "") for p in priorities],
            issue_types=sorted({t.get("name", "") for t in statuses}),
        )

    # ------------------------------------------------------------------
    # Current user
    # ------------------------------------------------------------------

    async def get_myself(self) -> JiraUser:
        """Return the authenticated user's profile."""
        data = await self._get("myself")
        return JiraUser(
            account_id=data["accountId"],
            display_name=data["displayName"],
            email=data.get("emailAddress", ""),
            active=bool(data.get("active", True)),
        )

    async def search_users(self, query: str, max_results: int = 10) -> Results:
        """Find users by display name or email.

        The bridge between a human saying "Vishwjeet" and JQL, which addresses
        people by ``accountId``. Matching on a display name inside JQL works
        until two people share one, or somebody is renamed; an accountId does
        not move.
        """
        # A bare array with no envelope, so OffsetPaging falls back to "a short
        # page means the end" — see its docstring for what that cannot answer.
        return await page_through(
            lambda params: self._get("user/search", query=query, **params),
            style=OffsetPaging(),
            limit=max_results,
            page_size=JIRA_PAGE_CAP,
            row=lambda item: JiraUser(
                account_id=item.get("accountId", ""),
                display_name=item.get("displayName", ""),
                email=item.get("emailAddress", ""),
                active=bool(item.get("active", True)),
            ),
        )


    async def resolve_user(self, name: str, cutoff: float = 0.6) -> UserLookup:
        """Find a person by name, tolerating a misspelling.

        Jira's user search is a substring match, not a fuzzy one, so a single
        wrong letter returns nothing at all — and an empty list reads as "no
        such person" rather than "try again". This retries with progressively
        shorter prefixes and ranks what comes back by similarity, so a typo
        produces a suggestion instead of a dead end.

        ``exact`` distinguishes a literal hit from a guess. Resolving a
        misspelling to the nearest human is reasonable for a read and reckless
        for a write, and that is the caller's decision, not this function's.
        """
        import difflib

        direct = await self.search_users(name, 20)
        if direct:
            return UserLookup(query=name, matches=direct, exact=True)

        # Shrink the query until Jira's substring match finds anything. Below
        # three characters the candidate set stops being about this person.
        candidates: list[JiraUser] = []
        for length in range(min(len(name), 5), 2, -1):
            candidates = await self.search_users(name[:length], 50)
            if candidates:
                break

        if not candidates:
            return UserLookup(
                query=name,
                note=f"No Jira user matches {name!r}, and no near match either.",
            )

        scored = sorted(
            (
                (difflib.SequenceMatcher(None, name.lower(), u.display_name.lower()).ratio(), u)
                for u in candidates
            ),
            key=lambda pair: -pair[0],
        )
        close = [u for score, u in scored if score >= cutoff]
        if not close:
            return UserLookup(
                query=name,
                matches=[u for _, u in scored[:3]],
                note=(
                    f"No user matches {name!r}. The closest names found were "
                    f"{[u.display_name for _, u in scored[:3]]} — none close enough "
                    "to assume. Confirm before using one."
                ),
            )

        return UserLookup(
            query=name,
            matches=close,
            note=(
                f"No exact match for {name!r}; {close[0].display_name!r} is the "
                "closest. Treat as a suggestion, not a fact, before writing."
            ),
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _flatten_adf(node: Any) -> str:
    """Pull the text out of an Atlassian Document Format tree."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    parts = [_flatten_adf(child) for child in node.get("content") or []]
    joined = "".join(parts)
    return joined + "\n" if node.get("type") == "paragraph" else joined


def _flatten_issue(raw: dict[str, Any]) -> JiraIssue:
    """Flatten a Jira issue API response into a typed model."""
    f = raw.get("fields", {})
    base_url = raw.get("self", "").split("/rest/")[0]
    key = raw.get("key", "")
    return JiraIssue(
        key=key,
        id=raw.get("id", ""),
        summary=f.get("summary", ""),
        status=(f.get("status") or {}).get("name", ""),
        assignee=(
            (f.get("assignee") or {}).get("displayName") or "Unassigned"
        ),
        priority=(f.get("priority") or {}).get("name", ""),
        issue_type=(f.get("issuetype") or {}).get("name", ""),
        project=(f.get("project") or {}).get("key", ""),
        labels=f.get("labels", []),
        created=f.get("created", ""),
        updated=f.get("updated", ""),
        url=f"{base_url}/browse/{key}",
    )


# Module-level singleton — lazy, reads env vars on first instantiation
_default_client: JiraClient | None = None


def get_default_client() -> JiraClient:
    """Return (or create) the module-level JiraClient from env vars."""
    global _default_client
    if _default_client is None:
        _default_client = JiraClient()
    return _default_client
