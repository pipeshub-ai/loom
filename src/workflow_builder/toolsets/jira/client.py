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

from workflow_builder.toolsets.jira.models import (
    Comment,
    CreatedIssue,
    JiraIssue,
    JiraProject,
    JiraProjectDetail,
    JiraUser,
    Transition,
)


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
    ) -> None:
        self._base_url = (base_url or os.environ.get("JIRA_URL", "")).rstrip("/")
        self._email = email or os.environ.get("JIRA_EMAIL", "")
        self._token = api_token or os.environ.get("JIRA_API_TOKEN", "")

        if not self._base_url:
            msg = "JIRA_URL is required (env var or base_url argument)"
            raise ValueError(msg)
        if not self._email or not self._token:
            msg = "JIRA_EMAIL and JIRA_API_TOKEN are required"
            raise ValueError(msg)

        credentials = base64.b64encode(
            f"{self._email}:{self._token}".encode()
        ).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> Any:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers, json=json)
            resp.raise_for_status()
            return resp.json()

    async def _put(self, path: str, json: dict[str, Any]) -> Any:
        import httpx

        url = f"{self._base_url}/rest/api/3/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(url, headers=self._headers, json=json)
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def search_issues(
        self,
        jql: str,
        max_results: int = 20,
        fields: list[str] | None = None,
    ) -> list[JiraIssue]:
        """Search using JQL and return a flat list of typed issues.

        Uses POST /rest/api/3/search/jql (Jira Cloud current endpoint).
        """
        default_fields = [
            "summary", "status", "assignee", "priority",
            "issuetype", "created", "updated", "description",
            "comment", "labels", "project",
        ]
        data = await self._post(
            "search/jql",
            {
                "jql": jql,
                "maxResults": max_results,
                "fields": fields or default_fields,
            },
        )
        return [_flatten_issue(i) for i in data.get("issues", [])]

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
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


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
