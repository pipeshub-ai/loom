"""Jira step functions for use inside LOOM workflows.

Each function is decorated with @step so it can be called via ctx.step().
The JiraClient is instantiated lazily on first call — credentials come from
JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN environment variables.

All functions return typed Pydantic models from ``models.py``.

Usage in a generated workflow::

    from loom.toolsets.jira.tools import (
        jira_search_issues,
        jira_create_issue,
    )

    issues = await ctx.step(jira_search_issues, "project = XYZ AND status = Open")
    new    = await ctx.step(jira_create_issue, "XYZ", "Fix login bug")
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.jira.client import JiraClient
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
from loom.toolsets.pagination import Results


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_search_issues(
    jql: str,
    max_results: int = 20,
    custom_fields: list[str] | None = None,
) -> Results[JiraIssue]:
    """Search Jira issues using JQL.

    Args:
        jql: JQL query string, e.g. ``"project = XYZ AND status = Open"``.
            Custom fields are filtered by their JQL name or ``cf[10016]``
            form, never by the ``customfield_10016`` id — the id matches
            nothing in JQL and does not error.
        max_results: Maximum number of issues to return (default 20).
        custom_fields: REST ids to fetch alongside the standard set, e.g.
            ``["customfield_10016"]``, or a system id JiraIssue has no
            attribute for (``["resolutiondate"]``). Resolve one with
            jira_resolve_field; they arrive on ``issue.custom_fields`` keyed
            by the id asked for, ``None`` when the issue has nothing there.

    Returns:
        List of JiraIssue models with: key, summary, status, assignee,
        priority, issue_type, project, labels, created, updated, url,
        custom_fields.
    """
    from loom.toolsets.factory import client_for


    return await (await client_for("jira", JiraClient)).search_issues(
        jql, max_results, custom_fields=custom_fields
    )


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_issue(
    issue_key: str, custom_fields: list[str] | None = None
) -> JiraIssue:
    """Fetch a single Jira issue by key.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        custom_fields: REST ids to fetch alongside the standard set, e.g.
            ``["customfield_10016"]``, or a system id JiraIssue has no
            attribute for (``["resolutiondate"]``). Resolve one with
            jira_resolve_field; they arrive on ``issue.custom_fields`` keyed
            by the id asked for, ``None`` when the issue has nothing there.

    Returns:
        A JiraIssue.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).get_issue(issue_key, custom_fields)


# No idempotency key: a retry after a timeout the service accepted files
# it twice, and nothing client-side can tell which happened.
@step(retry=Retry(max_attempts=1))
async def jira_create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Story",
    priority: str = "Medium",
    labels: list[str] | None = None,
    assignee_account_id: str | None = None,
    custom_fields: dict[str, Any] | None = None,
) -> CreatedIssue:
    """Create a new Jira issue.

    Args:
        project_key: Project key, e.g. ``"XYZ"``.
        summary: Issue title / summary.
        description: Longer description (plain text).
        issue_type: ``"Story"``, ``"Bug"``, ``"Task"``, ``"Epic"``, etc.
        priority: ``"Highest"``, ``"High"``, ``"Medium"``, ``"Low"``.
        labels: Optional list of label strings.
        assignee_account_id: Atlassian accountId, from jira_resolve_user.
            Not a display name.
        custom_fields: Keyed by REST id, in Jira's own value shape, e.g.
            ``{"customfield_10016": 5}`` for a number,
            ``{"customfield_10001": {"value": "Platform"}}`` for a select.
            Resolve an id with jira_resolve_field. A project that makes a
            custom field mandatory rejects a create without it, naming the
            field in the error.

    Returns:
        CreatedIssue with key, id, and browse url.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).create_issue(
        project_key,
        summary,
        description,
        issue_type,
        priority,
        labels,
        assignee_account_id,
        custom_fields,
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def jira_update_issue(
    issue_key: str,
    fields: dict[str, Any],
) -> JiraIssue:
    """Update fields on an existing Jira issue.

    ``fields`` is the Jira REST payload, not a friendly mapping — each value
    carries the shape Jira stores, and the wrong shape is a 400 rather than a
    coercion:

    - named entities are objects: ``{"priority": {"name": "High"}}``,
      ``{"assignee": {"accountId": "712020:abc"}}``
    - ``description`` is Atlassian Document Format, not a string
    - ``labels`` and other arrays are plain lists
    - a custom field is keyed by REST id: ``{"customfield_10016": 5}``

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        fields: Jira field ids to new values,
                e.g. ``{"priority": {"name": "High"}}``. Resolve a custom
                field's id with jira_resolve_field — never guess the number,
                it differs per instance.

    Returns:
        Updated JiraIssue.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).update_issue(issue_key, fields)


# No idempotency key: a retry after a timeout the service accepted files
# it twice, and nothing client-side can tell which happened.
@step(retry=Retry(max_attempts=1))
async def jira_add_comment(
    issue_key: str, comment: str
) -> Comment:
    """Add a comment to a Jira issue.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        comment: Plain-text comment body.

    Returns:
        Comment with id, author, and created timestamp.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).add_comment(issue_key, comment)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_transitions(
    issue_key: str,
) -> list[Transition]:
    """Get available status transitions for a Jira issue.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.

    Returns:
        List of Transition models with id and name.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).get_transitions(issue_key)


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def jira_transition_issue(
    issue_key: str,
    transition_name: str,
) -> JiraIssue:
    """Transition a Jira issue to a new status by transition name.

    Fetches available transitions first and matches by name
    (case-insensitive).

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        transition_name: Name of the transition, e.g. ``"In Progress"``.

    Returns:
        Updated JiraIssue after the transition.
    """
    from loom.toolsets.factory import client_for

    client = (await client_for("jira", JiraClient))
    transitions = await client.get_transitions(issue_key)
    match = next(
        (t for t in transitions if t.name.lower() == transition_name.lower()),
        None,
    )
    if match is None:
        available = [t.name for t in transitions]
        msg = (
            f"No transition '{transition_name}' on {issue_key}. "
            f"Available: {available}"
        )
        raise ValueError(msg)
    return await client.transition_issue(issue_key, match.id)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_list_projects() -> list[JiraProject]:
    """List all accessible Jira projects.

    Returns:
        List of JiraProject models with key, name, and id.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).list_projects()


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_search_users(
    query: str,
    max_results: int = 10,
) -> Results[JiraUser]:
    """Find Jira users by display name or email address.

    Use this before writing a JQL clause about a person. JQL addresses people by
    accountId; a display name works until two people share one or somebody is
    renamed, and then it silently matches nothing rather than failing.

    Args:
        query: Part of a display name or email, e.g. ``"a.person@x.com"``.
        max_results: Maximum users to return (default 10).

    Returns:
        List of JiraUser with account_id, display_name, email, active.
        Empty when nobody matches — which is worth distinguishing from
        "the person exists but has no matching issues".
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).search_users(query, max_results)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_myself() -> JiraUser:
    """Get the authenticated Jira user's profile.

    Returns:
        JiraUser with account_id, display_name, and email.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).get_myself()


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_resolve_user(name: str) -> UserLookup:
    """Find a person by name, tolerating a misspelling.

    Prefer this over jira_search_users when the name came from a human. Jira's
    search is a substring match, so one wrong letter returns nothing at all and
    an empty result reads as "no such person" instead of "check the spelling".

    Args:
        name: A display name or part of one, possibly misspelled.

    Returns:
        UserLookup with matches, exact (False when it is a near-miss guess),
        and note. Check ``exact`` before acting on a write: resolving a typo to
        the nearest human is fine for a read and reckless for an assignment.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).resolve_user(name)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_resolve_project(project_name: str) -> ProjectLookup:
    """Resolve a project's spoken name to the key JQL needs.

    A project has two identifiers a person might say — the key (``PA``) and the
    name (``Acme Platform``) — and ``project =`` accepts either, so call this
    before filtering on whichever one the request used. Jira answers a filter
    on a project that does not exist with zero issues and no error, which reads
    as an empty project rather than a bad filter.

    Args:
        project_name: A project key, a project name, or part of one.

    Returns:
        ProjectLookup with matches, exact, and note. More than one match means
        ambiguous, not "take the first".
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).resolve_project(project_name)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_resolve_epic(epic_name: str, project: str = "") -> EpicLookup:
    """Resolve an epic's name to its issue key.

    Use this whenever a request names an epic — "the billing epic", "the search
    epic". An epic is addressed in JQL by its issue key (``ACME-42``), never by
    the word anybody says, and the two are joined by nothing.

    An epic is the awkward container: it *is* an issue, so there is no endpoint
    listing epics and no way to look one up except a JQL search scoped to the
    epic issue type. That is what this does. Doing it inline instead — matching
    ``summary ~ "saas"`` across every issue — searches the whole site and
    returns whatever mentions the word.

    Args:
        epic_name: The epic's name as a person said it.
        project: Project key to search within, when it is known. Epic names
            repeat across projects, so this is usually what makes the answer
            single. Resolve it first with jira_resolve_project.

    Returns:
        EpicLookup with matches, exact, and note. Filter on
        ``matches[0].key`` only when exactly one matched; hand several to a
        ctx.agent() step to choose between.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).resolve_epic(epic_name, project)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_list_fields(
    query: str = "", max_results: int = 200
) -> Results[JiraField]:
    """List the custom fields this Jira instance defines.

    Custom fields are per-instance configuration: "Story Points" is
    ``customfield_10016`` here and ``customfield_10024`` next door. Nothing
    joins the display name to the id, so a guessed id either writes to the
    wrong field or is rejected outright.

    Args:
        query: Substring of the field name or description. Empty lists all.
        max_results: Maximum fields to return (default 200).

    Returns:
        Results of JiraField — check ``.complete``, since "not in the first
        200" is not the same as "does not exist". Each carries ``id`` (what a
        REST payload uses) and ``clause_names`` (what JQL accepts).
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).list_fields(query, max_results)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_resolve_field(field_name: str) -> FieldLookup:
    """Resolve a custom field's display name to the id a payload needs.

    Call this before putting any custom field in a JQL string, a
    ``custom_fields`` list, or an update payload. Prefer resolving once while
    authoring and writing the id into the code with the name in a comment —
    the mapping does not change between runs, and looking it up on every run
    re-answers a settled question.

    Args:
        field_name: The name a person uses, e.g. ``"Story Points"``.

    Returns:
        FieldLookup with ``matches``, ``exact``, and ``note``. ``exact=False``
        is a suggestion, not a fact — confirm it before writing to the field.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).resolve_field(field_name)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_project_metadata(project_key: str) -> ProjectMetadata:
    """List the status, priority, and issue-type names a project actually uses.

    Call this before filtering a search on either. Status and priority names are
    per-project configuration — "In Progress" and "High" are common defaults,
    not guarantees — and a JQL filter naming one the board does not have returns
    zero rows with no error, which reads as "no such work".

    Args:
        project_key: Project key, e.g. ``"QUES"``.

    Returns:
        ProjectMetadata with statuses, priorities, and issue_types.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).get_metadata(project_key)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_project(project_key: str) -> JiraProjectDetail:
    """Get one project's details.

    Args:
        project_key: Project key, e.g. ``"QUES"``.

    Returns:
        JiraProjectDetail with key, name, id, description, lead.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).get_project(project_key)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_comments(issue_key: str, max_results: int = 20) -> Results[Comment]:
    """Read an issue's comments.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        max_results: Maximum comments to return (default 20).

    Returns:
        List of Comment with id, author, created, and body as plain text.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).get_comments(issue_key, max_results)


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def jira_assign_issue(issue_key: str, account_id: str | None) -> JiraIssue:
    """Assign an issue, or unassign it.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        account_id: The assignee's accountId, from jira_resolve_user. Pass None
            to unassign. A display name will not work here.

    Returns:
        The updated JiraIssue.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).assign_issue(issue_key, account_id)


@step(retry=Retry(max_attempts=1))
async def jira_delete_issue(issue_key: str, delete_subtasks: bool = False) -> str:
    """Permanently delete an issue. There is no undo, and no retry.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        delete_subtasks: Delete its subtasks too. Jira refuses otherwise when
            the issue has any.

    Returns:
        The key that was deleted, so the journal records what went.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("jira", JiraClient)).delete_issue(issue_key, delete_subtasks)


# ---------------------------------------------------------------------------
# Auto-generated tool documentation
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    """Build JIRA_TOOL_DOCS from model schemas and function signatures.

    This keeps the docs DRY — they are derived from the actual
    Pydantic models and function signatures, not hand-written.
    """
    from loom.toolsets.jira.models import (
        Comment as _Comment,
    )
    from loom.toolsets.jira.models import (
        CreatedIssue as _CreatedIssue,
    )
    from loom.toolsets.jira.models import (
        JiraField as _JiraField,
    )
    from loom.toolsets.jira.models import (
        JiraIssue as _JiraIssue,
    )
    from loom.toolsets.jira.models import (
        JiraProject as _JiraProject,
    )
    from loom.toolsets.jira.models import (
        JiraUser as _JiraUser,
    )
    from loom.toolsets.jira.models import (
        Transition as _Transition,
    )

    def _fields(model: type[BaseModel]) -> str:
        """The field names a model declares, for the agent-facing docs.

        ``type[BaseModel]`` rather than ``type``: the bare form accepts any
        class and then calls a Pydantic classmethod on it, so passing the wrong
        thing is an AttributeError from inside a docs generator rather than an
        error where the mistake was made.
        """
        props = model.model_json_schema().get("properties", {})
        return ", ".join(props)

    return f"""\
## Available Jira Tools

Import: from loom.toolsets.jira.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN

Tools return typed Pydantic models (not plain dicts) — use attribute access:
issue.key, issue.status, project.name. The exception is jira_delete_issue,
which returns a confirmation string.

Three things that make a JQL query return nothing rather than fail:

  - Status and priority names differ per project. "In Progress" and "High"
    are common defaults, not guarantees — a board may use "Blocked",
    "Highest", or names in another language. When a filtered search comes
    back empty, search without the filter and report the values that do
    exist, so an empty result can be told apart from a wrong guess.
  - People are addressed by accountId. Resolve a name with
    jira_search_users first.
  - Containers are addressed by key, never by the word anybody says. "the
    billing epic" is ACME-42 and "Acme Platform" is ACME. Resolve with
    jira_resolve_epic / jira_resolve_project before filtering — see below.
  - Custom fields have two names and neither is the one on screen. Resolve
    with jira_resolve_field before using one anywhere — see below.

### Custom fields

"Story Points", "Sprint", "Epic Link" and anything else a site added are
per-instance configuration. The display name is stable; the identifier is not
— Story Points is customfield_10016 on one site and customfield_10024 on the
next. Never write a customfield_NNNNN literal you have not resolved.

Each field has TWO identifiers and they are not interchangeable:

  - field.id ............ "customfield_10016" — REST payloads: the
                          custom_fields= list on a read, the fields= dict on
                          an update, the custom_fields= dict on a create.
  - field.clause_names .. ["Story Points", "cf[10016]"] — JQL, and only JQL.

Putting the REST id in JQL matches nothing and does not error. Putting a
clause name in an update payload is a 400.

Resolve once, then write the id into the code with the name beside it — the
mapping does not change between runs:

    found = await ctx.step(jira_resolve_field, "Story Points")
    # found.matches[0].id -> "customfield_10016"

    # ...then, in the workflow:
    issues = await ctx.step(
        jira_search_issues,
        'project = XYZ AND "Story Points" > 5',      # JQL: clause name
        custom_fields=["customfield_10016"],          # payload: REST id
    )
    for issue in issues:
        points = issue.custom_fields.get("customfield_10016")

Values arrive in Jira's own shape, unflattened: a number field is a number, a
select is {{"value": "High", "id": "10001"}}, a user is an account object. A
field that was asked for and is empty on the issue comes back as None, which
is not the same as the key being absent — that means nobody asked.

System fields go through the same door when JiraIssue has no attribute for
them. jira_resolve_field says which: it resolves "Due date" to `duedate` and
reports that issue.due_date carries it, and "Resolution date" to
`resolutiondate` with no attribute to name, so:

    issues = await ctx.step(
        jira_search_issues, "project = XYZ AND resolved >= -7d",
        custom_fields=["resolutiondate"],          # a system id, no prefix
    )
    resolved_at = issues[0].custom_fields.get("resolutiondate")

### Tools

jira_search_issues(jql: str, max_results: int = 20, \
custom_fields: list[str] | None = None) -> Results[JiraIssue]
  Search using a JQL string. Returns a Results list — a list subclass with
  .complete (bool: False when the source had more and max_results cut it off),
  .total (int | None: how many matched in all), and .summary() (str).
  Always check .complete when the set could be large. There is no cursor
  argument — the toolset pages Jira internally to fill max_results, so raise
  max_results or narrow the JQL.
  custom_fields takes REST ids and adds them to what is fetched.
  JiraIssue fields: {_fields(_JiraIssue)}
  Examples:
    issues = await ctx.step(jira_search_issues, \
"project = XYZ AND status = \\"To Do\\"")
    issues = await ctx.step(jira_search_issues, \
"assignee = currentUser() AND sprint in openSprints()")
    issues = await ctx.step(jira_search_issues, \
"issuetype = Bug AND priority = High", 50)
    # Check coverage:
    if not issues.complete:
        n, t = len(issues), issues.total
        await ctx.report(f"showing {{n}} of {{t}}")

jira_get_issue(issue_key: str, custom_fields: list[str] | None = None) \
-> JiraIssue
  Fetch a single issue by key.
    issue = await ctx.step(jira_get_issue, "PROJ-123")
    print(issue.summary, issue.status)
    # with custom fields, by resolved REST id:
    issue = await ctx.step(jira_get_issue, "PROJ-123", \
["customfield_10016"])
    points = issue.custom_fields.get("customfield_10016")

jira_create_issue(project_key, summary, description="", \
issue_type="Story", priority="Medium", labels=None, \
assignee_account_id=None, custom_fields=None) -> CreatedIssue
  Create an issue. assignee_account_id is an accountId from
  jira_resolve_user, not a name. custom_fields is keyed by REST id.
  CreatedIssue fields: {_fields(_CreatedIssue)}
    created = await ctx.step(jira_create_issue, "XYZ", "Add login", \
"SSO support", "Story", "High")
    print(created.key, created.url)

jira_update_issue(issue_key: str, fields: dict) -> JiraIssue
  Update Jira fields. Returns updated issue. `fields` is the raw Jira REST
  payload, and the wrong shape is a 400 rather than a coercion:
    named entities are objects   {{"priority": {{"name": "High"}}}}
                                 {{"assignee": {{"accountId": "712020:abc"}}}}
    description is ADF           not a plain string
    arrays are plain lists       {{"labels": ["backend"]}}
    custom fields by REST id     {{"customfield_10016": 5}}
    updated = await ctx.step(jira_update_issue, "PROJ-123", \
{{"priority": {{"name": "High"}}}})

jira_add_comment(issue_key: str, comment: str) -> Comment
  Add a plain-text comment.
  Comment fields: {_fields(_Comment)}
    c = await ctx.step(jira_add_comment, "PROJ-123", "Fixed in v2")
    print(c.id, c.author)

jira_get_transitions(issue_key: str) -> list[Transition]
  List available transitions.
  Transition fields: {_fields(_Transition)}

jira_transition_issue(issue_key: str, transition_name: str) -> JiraIssue
  Move issue to new status by name (case-insensitive).
    updated = await ctx.step(jira_transition_issue, "PROJ-123", \
"In Progress")

jira_list_projects() -> list[JiraProject]
  List all accessible projects.
  JiraProject fields: {_fields(_JiraProject)}
    projects = await ctx.step(jira_list_projects)
    for p in projects: print(p.key, p.name)

jira_resolve_project(project_name: str) -> ProjectLookup
  Resolve a project's spoken name to its key. "project = Acme Platform"
  matches no project, and Jira answers with zero issues and no error — which
  reads as an empty project rather than a bad filter.
    found = await ctx.step(jira_resolve_project, "Acme Platform")
    if len(found.matches) == 1:
        key = found.matches[0].key          # "ACME"

jira_resolve_epic(epic_name: str, project: str = "") -> EpicLookup
  Resolve an epic's name to its issue key. Call this for any request naming
  an epic. An epic IS an issue, so there is no endpoint listing epics and no
  lookup but this one — writing 'summary ~ "billing"' inline instead
  searches every issue on the site and returns whatever mentions the word.
    found = await ctx.step(jira_resolve_epic, "billing", "ACME")
    if len(found.matches) == 1:
        epic = found.matches[0].key         # "ACME-42"
    elif found.matches:
        # Several. Ambiguous — choose, do not take the first.
        epic = await ctx.agent(f"Which epic is 'billing'? {{found.note}}")
  Then filter on the key, never on the name:
    issues = await ctx.step(
        jira_search_issues,
        f'parentEpic = {{epic}} AND duedate < now()',
    )

jira_resolve_user(name: str) -> UserLookup
  Find a person by name, tolerating a misspelling. Prefer this when the
  name came from a human — Jira's search is a substring match, so one
  wrong letter returns nothing and reads as "no such person".
    found = await ctx.step(jira_resolve_user, "Viswajeet")
    if found.matches and found.exact:
        aid = found.matches[0].account_id
    elif found.matches:
        # A guess. Fine to read with, confirm before writing.
        aid = found.matches[0].account_id

jira_list_fields(query: str = "", max_results: int = 200) \
-> Results[JiraField]
  Every custom field this instance defines. Returns a Results list — check
  .complete, since "not in the first 200" is not "does not exist".
  JiraField fields: {_fields(_JiraField)}
    fields = await ctx.step(jira_list_fields, "points")

jira_resolve_field(field_name: str) -> FieldLookup
  Resolve a custom field's display name to its REST id. Call before using a
  custom field in JQL, in custom_fields, or in an update payload.
    found = await ctx.step(jira_resolve_field, "Story Points")
    if found.matches and found.exact:
        field_id = found.matches[0].id      # "customfield_10016"
        clause = found.matches[0].clause_names[0]   # for JQL
    elif found.matches:
        # A guess. Fine to read with, confirm before writing.
        field_id = found.matches[0].id

jira_get_project_metadata(project_key: str) -> ProjectMetadata
  The status, priority, and issue-type names this project actually uses.
  Call before filtering: a JQL filter naming a status the board does not
  have returns zero rows with no error.
    meta = await ctx.step(jira_get_project_metadata, "QUES")
    print(meta.statuses, meta.priorities)

jira_get_project(project_key: str) -> JiraProjectDetail
jira_get_comments(issue_key: str, max_results: int = 20) -> Results[Comment]
  Returns a Results list (.complete, .total, .summary()).
jira_assign_issue(issue_key: str, account_id: str | None) -> JiraIssue
  Takes an accountId, not a name. None unassigns.
jira_delete_issue(issue_key: str, delete_subtasks: bool = False) -> str
  Permanent. Not retried.

jira_search_users(query: str, max_results: int = 10) -> Results[JiraUser]
  Resolve a person's name to an accountId before using them in JQL.
  Returns a Results list (.complete, .total, .summary()).
  JiraUser fields: {_fields(_JiraUser)}
    users = await ctx.step(jira_search_users, "<display name>")
    if users:
        jql = f'assignee = "{{users[0].account_id}}" AND status = "In Progress"'
        issues = await ctx.step(jira_search_issues, jql)

jira_get_myself() -> JiraUser
  Get authenticated user.
  JiraUser fields: {_fields(_JiraUser)}
    me = await ctx.step(jira_get_myself)
    print(me.display_name, me.email)
"""


JIRA_TOOL_DOCS: str = _build_tool_docs()
