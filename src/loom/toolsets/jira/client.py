"""Lazy httpx-based Jira REST API v3 client.

No ``jira`` pip package required — pure httpx over the REST API.
Credentials are passed in, never read here — see
``loom.toolsets.factory.client_for``, which builds this from whatever the run's
``ToolsetSession`` resolved.

All public methods return typed Pydantic models from ``models.py``.
A future iteration may auto-generate this client from the official
Jira Cloud OpenAPI 3.1 spec at
https://developer.atlassian.com/cloud/jira/platform/rest/v3/
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.atlassian import api_root, is_bearer, resolve_cloud_id
from loom.toolsets.jira.models import (
    Comment,
    CreatedIssue,
    EpicLookup,
    FieldLookup,
    JiraField,
    JiraIssue,
    JiraProject,
    JiraProjectDetail,
    JiraUser,
    ProjectLookup,
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

#: What a search asks for when the caller names nothing. Jira returns *only*
#: what ``fields=`` lists, so this is the whole of what a ``JiraIssue`` can be
#: built from — and why a custom field is absent until something asks for it.
DEFAULT_ISSUE_FIELDS = (
    "summary", "status", "assignee", "priority",
    "issuetype", "created", "updated", "duedate", "description",
    "comment", "labels", "project",
)

# Jira REST id -> the JiraIssue attribute that carries it. What `resolve_field`
# reads to say whether a system field needs a ``custom_fields`` entry: the ones
# absent here (``resolutiondate``, ``parent``, ``fixVersions``, ...) are fetched
# and returned only when a read names them.
SYSTEM_FIELD_ATTRS = {
    "summary": "summary",
    "status": "status",
    "assignee": "assignee",
    "priority": "priority",
    "issuetype": "issue_type",
    "project": "project",
    "labels": "labels",
    "created": "created",
    "updated": "updated",
    "duedate": "due_date",
}


class JiraError(WorkflowError):
    """A Jira request failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self, message: str, *, status: int = 0, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields or {}
        """Jira's per-field complaints, field id -> what is wrong with it.

        The actionable half of a 400. Setting an unknown or unscreened custom
        field is reported here and nowhere else, so discarding it leaves a
        caller — or a repair loop — with "400 Bad Request" and no field name.
        """


class JiraPermanentError(JiraError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class JiraAuthError(JiraPermanentError):
    """Missing, malformed, expired, or revoked credentials."""


class JiraNotFound(JiraPermanentError):  # noqa: N818 - names a state
    """No such issue, project, or field — or the token cannot see it.

    Jira returns 404 for a resource the credential lacks permission on, so
    this reads as "not visible to you" rather than strictly "absent".
    """


class JiraRateLimited(JiraError):  # noqa: N818 - names a state
    """Quota spent. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


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
        base_url: str,
        email: str = "",
        api_token: str = "",
        *,
        token_source: Any = None,
    ) -> None:
        """Values in, nothing read from the environment.

        ``base_url`` is required because there is no sensible default: every
        Jira site answers on its own host, and a client built without one
        produces 404s that read as missing issues.

        ``email``/``api_token`` are optional because they are one of *two* ways
        to authenticate — a ``token_source`` supplying a bearer token is the
        other, and a deployment that has connected one needs neither. Whether
        either is present cannot be decided here without an await, so it is
        decided at the first request, where the answer is knowable.
        """
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._token = api_token
        self._token_source = token_source
        self._cloud: str | None = None

        if not self._base_url:
            raise ValueError("base_url is required")

    async def _headers(self) -> dict[str, str]:
        """Basic auth from email/token, or a CredentialStore-issued bearer token.

        The store is checked first and on every call — never cached — so a
        run that connected 'jira' after this client was constructed, or
        whose stored token the store has since refreshed, is never stuck
        with what was true when ``__init__`` ran. Falls back to Basic auth
        when no store is bound or it has nothing under ``credential_name``,
        which is exactly today's only behaviour.
        """
        token = await self._token_source.token() if self._token_source else None
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
            "jira has no credential: supply JIRA_EMAIL and JIRA_API_TOKEN, or "
            "connect one with `loom connect jira`"
        )

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> Any:
        """One place where a Jira response becomes a value or an exception.

        Every method went through ``raise_for_status`` before, which discards
        the body — and Jira puts the only useful part of a 400 in the body,
        keyed by the field that caused it. A caller setting an unknown custom
        field got ``Client error '400 Bad Request' for url ...`` and no way to
        learn which field, while three retries were spent re-asking.
        """
        import httpx

        headers = await self._headers()
        url = f"{await self._api_root(headers)}/rest/api/3/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, url, headers=headers, params=_clean(params), json=json
            )
        if resp.status_code >= 400:
            raise _classify(resp)
        if not resp.content:
            return {}
        return resp.json()

    async def _api_root(self, headers: dict[str, str]) -> str:
        """Where a request goes, which depends on how it is authenticated.

        The rule and its reasoning live in :mod:`loom.toolsets.atlassian`,
        shared with Confluence: it is the provider's rule rather than Jira's,
        and a second copy is a second thing to fix the next time it changes.
        """
        if not is_bearer(headers):
            return self._base_url
        if self._cloud is None:
            self._cloud = await resolve_cloud_id(
                headers["Authorization"],
                site_url=self._base_url,
                classify=_classify,
            )
        return api_root("jira", self._cloud)

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        return await self._request("POST", path, json=json)

    async def _put(self, path: str, json: dict[str, Any]) -> Any:
        return await self._request("PUT", path, json=json)

    async def _delete(self, path: str, **params: Any) -> None:
        await self._request("DELETE", path, params=params)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def search_issues(
        self,
        jql: str,
        max_results: int = 20,
        fields: list[str] | None = None,
        custom_fields: list[str] | None = None,
    ) -> Results[Any]:
        """Search using JQL and return typed issues, following every page.

        Uses POST /rest/api/3/search/jql (Jira Cloud current endpoint), which
        caps a page at 100 however large a ``maxResults`` you send — and says
        nothing when it does. Asking for 500 and receiving 100 with a 200 OK is
        the failure this loop exists to prevent.

        The result is a list, so existing callers are unaffected; check
        ``.complete`` to find out whether ``max_results`` cut it off.

        ``custom_fields`` are REST ids (``customfield_10016``) *appended* to
        the defaults, where ``fields`` replaces them. Jira returns only what
        was asked for, so a custom field absent from both arrives as absent
        data rather than as an error — resolve the id with
        :meth:`resolve_field` rather than guessing it.
        """
        requested = list(custom_fields or [])
        asked = list(fields or DEFAULT_ISSUE_FIELDS)
        asked += [f for f in requested if f not in asked]

        return await page_through(
            lambda params: self._post(
                "search/jql",
                {"jql": jql, "fields": asked, **params},
            ),
            style=TokenPaging(
                items="issues",
                token_param="nextPageToken",
                last_field="isLast",
                total_field="total",
            ),
            limit=max_results,
            page_size=JIRA_PAGE_CAP,
            row=lambda data: _flatten_issue(data, requested, self._base_url),
        )

    async def get_issue(
        self, issue_key: str, custom_fields: list[str] | None = None
    ) -> JiraIssue:
        """Fetch a single issue by key, e.g. ``'PROJ-123'``.

        ``custom_fields`` names REST ids to fetch alongside the standard set;
        they arrive on :attr:`JiraIssue.custom_fields`.
        """
        params: dict[str, Any] = {}
        requested = list(custom_fields or [])
        if requested:
            asked = list(DEFAULT_ISSUE_FIELDS)
            asked += [f for f in requested if f not in asked]
            params["fields"] = ",".join(asked)
        data = await self._get(f"issue/{issue_key}", **params)
        return _flatten_issue(data, requested, self._base_url)

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Story",
        priority: str = "Medium",
        labels: list[str] | None = None,
        assignee_account_id: str | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> CreatedIssue:
        """Create a new issue. Returns key, id, and browse URL.

        ``custom_fields`` is merged in after the named arguments, keyed by
        REST id and carrying Jira's own value shape — a number for a number
        field, ``{"value": "High"}`` for a select. A project that makes a
        custom field mandatory cannot be created in without it, and Jira
        reports that as a 400 naming the field.
        """
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
        if custom_fields:
            fields.update(custom_fields)

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

    async def get_comments(self, issue_key: str, max_results: int = 20) -> Results[Any]:
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

    async def search_users(self, query: str, max_results: int = 10) -> Results[Any]:
        """Find users by display name or email.

        The bridge between a human saying a colleague's name and JQL, which addresses
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


    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------

    async def _catalogue(self) -> list[dict[str, Any]]:
        """``GET /field`` — every field it will admit to, system ones included.

        Only this endpoint carries ``clauseNames``, and only it covers system
        fields — but on an instance with no company-managed project it omits
        app-created custom fields entirely (about 45 of 85 in the report that
        prompted this), so it cannot be the source of the custom field list.
        It is the enrichment; ``field/search`` is the list.
        """
        rows = await self._get("field")
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]

    @staticmethod
    def _clause_names(catalogue: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Field id -> the names JQL accepts for it."""
        return {
            str(row["id"]): list(row.get("clauseNames") or []) for row in catalogue
        }

    async def list_fields(
        self, query: str = "", max_results: int = 200
    ) -> Results[Any]:
        """Every custom field this instance defines, optionally name-filtered.

        Over ``field/search`` rather than ``field``: the paginated endpoint
        returns app-created custom fields regardless of which project types
        exist, where the plain one silently omits them on an instance with
        only team-managed projects. Silently is the operative word — the
        missing field looks like a field nobody created.

        ``query`` is Jira's own substring match over name and description.
        Check ``.complete``: an instance with a thousand custom fields will
        not fit one call, and "not in the first 200" is not "does not exist".
        """
        clause_names = self._clause_names(await self._catalogue())

        def request(params: dict[str, Any]) -> Any:
            return self._get(
                "field/search", type="custom", query=query or None, **params
            )

        return await page_through(
            request,
            style=OffsetPaging(items="values", total_field="total"),
            limit=max_results,
            page_size=JIRA_PAGE_CAP,
            row=lambda item: _field_row(item, clause_names),
        )

    @staticmethod
    def _system_field_note(wanted: str, field_id: str) -> str:
        """What a caller has to do to read a system field, per field.

        JiraIssue carries ten of them and no more, so a blanket "already
        carried" sent a read for a due date away with nothing to ask for and
        an empty mapping to read from.
        """
        attr = SYSTEM_FIELD_ATTRS.get(field_id)
        if attr:
            return (
                f"{wanted!r} is a system field ({field_id}), not a custom one. "
                f"JiraIssue carries it as issue.{attr} — no custom_fields entry "
                "is needed to read it."
            )
        return (
            f"{wanted!r} is a system field ({field_id}), not a custom one, and "
            "JiraIssue has no attribute for it. Read it the same way as a "
            f"custom field: custom_fields=[{field_id!r}] on the search or get, "
            f"then issue.custom_fields[{field_id!r}]. In JQL use the clause "
            "name, not the id."
        )

    async def resolve_field(
        self, field_name: str, cutoff: float = 0.6
    ) -> FieldLookup:
        """Resolve a field's display name to the id a REST payload needs.

        The field equivalent of :meth:`resolve_user`, and the same failure
        without it: "Story Points" is a name a person uses and
        ``customfield_10016`` is what Jira stores, the number differs per
        instance, and a guess either 400s on a write or filters on nothing.

        Near matches are returned labelled rather than silently accepted.
        "Story Points" and "Story point estimate" are different fields on
        instances that have both, and picking one writes to the wrong column.
        """
        import difflib

        wanted = field_name.strip()
        found = await self.list_fields(wanted, max_results=200)

        exact = [f for f in found if f.name.lower() == wanted.lower()]
        if exact:
            note = ""
            if len(exact) > 1:
                note = (
                    f"{len(exact)} fields are named {wanted!r} "
                    f"({', '.join(f.id for f in exact)}). Jira allows duplicate "
                    "names; pick by id, do not assume the first."
                )
            return FieldLookup(query=wanted, matches=exact, exact=True, note=note)

        # A system field before a fuzzy guess. `field/search` returns custom
        # fields only, so "Summary" finds nothing there and would otherwise be
        # scored against every custom field on the site — confidently
        # returning whichever happened to look most like it.
        catalogue = await self._catalogue()
        clause_names = self._clause_names(catalogue)
        system = [
            _field_row(row, clause_names)
            for row in catalogue
            if not row.get("custom") and (row.get("name") or "").lower() == wanted.lower()
        ]
        if system:
            return FieldLookup(
                query=wanted,
                matches=system,
                exact=True,
                note=self._system_field_note(wanted, system[0].id),
            )

        # Jira's `query` is a substring match, so a wrong word order or a
        # typo returns nothing at all. Sweep the catalogue and rank it.
        candidates = list(found) or list(await self.list_fields(max_results=1000))
        if not candidates:
            return FieldLookup(
                query=wanted,
                note=(
                    f"No field matches {wanted!r}, and no near match either. "
                    "Check the spelling against jira_list_fields rather than "
                    "assuming an id."
                ),
            )

        scored = sorted(
            (
                (difflib.SequenceMatcher(None, wanted.lower(), f.name.lower()).ratio(), f)
                for f in candidates
            ),
            key=lambda pair: -pair[0],
        )
        close = [f for score, f in scored if score >= cutoff]
        if not close:
            return FieldLookup(
                query=wanted,
                matches=[f for _, f in scored[:3]],
                note=(
                    f"No field is named {wanted!r}. The closest were "
                    f"{[f.name for _, f in scored[:3]]} — none close enough to "
                    "assume. Confirm which one before writing to it."
                ),
            )

        return FieldLookup(
            query=wanted,
            matches=close,
            note=(
                f"No exact match for {wanted!r}; {close[0].name!r} "
                f"({close[0].id}) is the closest. Treat as a suggestion, not a "
                "fact, before writing."
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

    async def resolve_project(
        self, name: str, cutoff: float = 0.6
    ) -> ProjectLookup:
        """Resolve a project's spoken name to the key JQL needs.

        The project equivalent of :meth:`resolve_user`. A project has two
        identifiers a person might say — the key (``ACME``) and the name
        (``Acme Platform``) — and JQL's ``project =`` accepts either, so an exact
        hit on one is authoritative and needs no guessing.

        What it protects against is the other case: ``project = "acme"``
        matches no project, and Jira answers a filter on a nonexistent project
        with zero issues rather than an error. That is indistinguishable from a
        project with nothing in it.
        """
        import difflib

        wanted = name.strip()
        folded = wanted.lower()
        projects = await self.list_projects()

        exact = [
            p for p in projects
            if p.key.lower() == folded or p.name.lower() == folded
        ]
        if exact:
            return ProjectLookup(query=wanted, matches=exact, exact=True)

        contains = [p for p in projects if folded in p.name.lower()]
        if len(contains) == 1:
            return ProjectLookup(
                query=wanted,
                matches=contains,
                note=(
                    f"{wanted!r} is not a project key or full name; "
                    f"{contains[0].name!r} ({contains[0].key}) is the only one "
                    "containing it."
                ),
            )
        if len(contains) > 1:
            return ProjectLookup(
                query=wanted,
                matches=contains,
                note=(
                    f"{len(contains)} projects contain {wanted!r}: "
                    f"{', '.join(f'{p.name} ({p.key})' for p in contains)}. "
                    "Pick one before filtering; do not assume the first."
                ),
            )

        scored = sorted(
            (
                (
                    difflib.SequenceMatcher(
                        None, folded, p.name.lower()
                    ).ratio(),
                    p,
                )
                for p in projects
            ),
            key=lambda pair: -pair[0],
        )
        close = [p for score, p in scored if score >= cutoff]
        if not close:
            return ProjectLookup(
                query=wanted,
                matches=[p for _, p in scored[:3]],
                note=(
                    f"No project is named {wanted!r}. The closest are "
                    f"{[p.name for _, p in scored[:3]]} — none close enough to "
                    "assume. It may name something else: an epic, a board, or "
                    "a label."
                ),
            )
        return ProjectLookup(
            query=wanted,
            matches=close,
            note=(
                f"No exact match for {wanted!r}; {close[0].name!r} "
                f"({close[0].key}) is the closest. Confirm before relying on it."
            ),
        )

    async def resolve_epic(
        self, name: str, project: str = "", max_results: int = 20
    ) -> EpicLookup:
        """Resolve an epic's name to its issue key.

        The one container with no endpoint of its own: an epic **is** an issue,
        so there is nothing to list and the only lookup is a JQL search scoped
        to the epic issue type. That is what this is — ``issuetype = Epic AND
        summary ~ <name>`` — and scoping it is what makes it a resolution
        rather than the free-text guess it superficially resembles. Without
        that scope the same query searches every issue on the site and returns
        whatever mentions the word.

        Pass ``project`` when the epic's project is already known: epic names
        repeat across projects far more than project names repeat, so the
        scope is usually what makes the answer single.
        """
        wanted = name.strip()
        clauses = ["issuetype = Epic", f"summary ~ {_jql_literal(wanted)}"]
        if project.strip():
            clauses.insert(0, f"project = {_jql_literal(project.strip())}")
        found = await self.search_issues(
            " AND ".join(clauses) + " ORDER BY updated DESC",
            max_results=max_results,
        )

        rows = list(found)
        exact = [i for i in rows if (i.summary or "").lower() == wanted.lower()]
        if len(exact) == 1:
            return EpicLookup(query=wanted, matches=exact, exact=True)
        if len(exact) > 1:
            return EpicLookup(
                query=wanted,
                matches=exact,
                exact=True,
                note=(
                    f"{len(exact)} epics are named exactly {wanted!r} "
                    f"({', '.join(i.key for i in exact)}). Pick one — Jira does "
                    "not require epic names to be unique."
                ),
            )
        if not rows:
            return EpicLookup(
                query=wanted,
                note=(
                    f"No epic's summary matches {wanted!r}"
                    + (f" in project {project!r}" if project.strip() else "")
                    + ". It may name a different kind of container — a project, "
                    "a board, a label, a component — or it may be subject "
                    "matter rather than a thing."
                ),
            )
        if len(rows) == 1:
            return EpicLookup(
                query=wanted,
                matches=rows,
                note=(
                    f"One epic mentions {wanted!r}: {rows[0].key} "
                    f"({rows[0].summary!r}). Its name is not exactly the word "
                    "asked for — confirm it is the one meant."
                ),
            )
        return EpicLookup(
            query=wanted,
            matches=rows,
            note=(
                f"{len(rows)} epics mention {wanted!r}: "
                + ", ".join(f"{i.key} ({i.summary})" for i in rows[:5])
                + ". None is named exactly that, so this is ambiguous — choose "
                "between them rather than taking the first."
            ),
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _jql_literal(value: str) -> str:
    """*value* as a quoted JQL string.

    JQL takes backslash escapes inside double quotes, so a name carrying either
    character has to be escaped rather than interpolated. Unescaped, a quote
    ends the string early and the remainder is parsed as JQL — which fails
    loudly on most inputs and, on the wrong one, silently changes the query.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _flatten_adf(node: Any) -> str:
    """Pull the text out of an Atlassian Document Format tree."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    parts = [_flatten_adf(child) for child in node.get("content") or []]
    joined = "".join(parts)
    return joined + "\n" if node.get("type") == "paragraph" else joined


def _clean(params: Any) -> Any:
    """Drop unset filters — httpx would otherwise send the string ``"None"``."""
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> JiraError:
    """Turn a failed response into the narrowest error that fits.

    Jira reports a bad field twice over: a sentence in ``errorMessages`` and a
    map in ``errors`` keyed by the field id. Both go into the message, and the
    map is kept on the exception, because "which field" is the only part a
    caller can act on.
    """
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    messages = [str(m) for m in (body.get("errorMessages") or [])]
    raw_fields = body.get("errors")
    fields = (
        {str(k): str(v) for k, v in raw_fields.items()}
        if isinstance(raw_fields, dict)
        else {}
    )
    detail = "; ".join(
        [*messages, *(f"{k}: {v}" for k, v in fields.items())]
    ) or (response.text or "").strip()[:300]
    message = f"Jira {status}: {detail or 'request failed'}"

    if status == 429:
        return JiraRateLimited(
            message,
            status=status,
            fields=fields,
            retry_after=float(response.headers.get("Retry-After", 0) or 0),
        )
    if status == 401:
        return JiraAuthError(message, status=status, fields=fields)
    if status == 404:
        return JiraNotFound(message, status=status, fields=fields)
    if 400 <= status < 500:
        return JiraPermanentError(message, status=status, fields=fields)
    return JiraError(message, status=status, fields=fields)


def _field_row(
    item: dict[str, Any], clause_names: dict[str, list[str]] | None = None
) -> JiraField:
    """One row of ``field``/``field/search`` as a typed model.

    ``field/search`` carries no ``clauseNames``, so they are taken from the
    ``field`` catalogue where it knows the id and synthesised where it does
    not: JQL accepts a custom field by display name and by ``cf[10016]``, and
    an empty list would read as "this field cannot be used in JQL".
    """
    schema = item.get("schema") or {}
    field_id = str(item.get("id", ""))
    name = item.get("name") or ""
    custom_id = schema.get("customId")
    known = (clause_names or {}).get(field_id)
    if known:
        clauses = list(known)
    else:
        clauses = [name] if name else []
        if custom_id is not None:
            clauses.append(f"cf[{custom_id}]")
    return JiraField(
        id=field_id,
        name=name,
        custom=bool(item.get("custom", schema.get("custom") is not None)),
        field_type=str(schema.get("type", "") or ""),
        custom_id=int(custom_id) if isinstance(custom_id, int) else None,
        clause_names=clauses,
    )


def _flatten_issue(
    raw: dict[str, Any], requested: Sequence[str] = (), site_url: str = ""
) -> JiraIssue:
    """Flatten a Jira issue API response into a typed model.

    ``site_url`` is where a *person* reads this issue, and it is passed in
    rather than read out of the response. Jira echoes the request host in
    ``self``, which is the site host under Basic auth and
    ``api.atlassian.com/ex/jira/{cloudId}`` under OAuth — so deriving the browse
    link from it produced ``https://api.atlassian.com/ex/jira/…/browse/PA-1786``,
    an API endpoint rendered as a link for somebody to click. It was only ever
    right because the two hosts happened to be the same thing, which stopped
    being true the moment 3LO tokens started working.

    ``create_issue`` already built its URL from the client's own ``base_url``
    and was correct throughout; this brings the other two into line with it.

    ``requested`` is what the read named in ``custom_fields=``; each one is
    carried back on :attr:`JiraIssue.custom_fields` under the id it was asked
    for. Keying that off a ``customfield_`` prefix instead was the bug this
    argument exists to close: ``jira_resolve_field`` resolves a *system* field
    to a bare id (``duedate``, ``resolutiondate``), the client duly fetched it,
    and the flattener then dropped it — a read that asked for a due date got
    an empty mapping and no error.
    """
    f = raw.get("fields", {})
    # Falls back to the response only when no site was supplied, so a caller
    # that forgets still gets a link rather than a bare path.
    base_url = (site_url or raw.get("self", "").split("/rest/")[0]).rstrip("/")
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
        due_date=f.get("duedate") or "",
        url=f"{base_url}/browse/{key}",
        # Only present when the read asked for them. Left in Jira's own shape:
        # a select is {"value": ..., "id": ...}, a user an account object, and
        # flattening those would decide, wrongly and per instance, which half
        # the caller wanted.
        custom_fields={
            **{k: v for k, v in f.items() if k.startswith("customfield_")},
            # Present with a None value when the field was asked for and the
            # issue has nothing in it — "asked and empty" and "never asked"
            # are different answers and a caller acts on them differently.
            **{k: f.get(k) for k in requested if k in f},
        },
    )

