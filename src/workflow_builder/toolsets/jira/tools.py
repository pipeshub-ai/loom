"""Jira step functions for use inside LOOM workflows.

Each function is decorated with @step so it can be called via ctx.step().
The JiraClient is instantiated lazily on first call — credentials come from
JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN environment variables.

All functions return typed Pydantic models from ``models.py``.

Usage in a generated workflow::

    from workflow_builder.toolsets.jira.tools import (
        jira_search_issues,
        jira_create_issue,
    )

    issues = await ctx.step(jira_search_issues, "project = XYZ AND status = Open")
    new    = await ctx.step(jira_create_issue, "XYZ", "Fix login bug")
"""

from __future__ import annotations

from workflow_builder import Retry, step
from workflow_builder.toolsets.jira.models import (
    Comment,
    CreatedIssue,
    JiraIssue,
    JiraProject,
    JiraUser,
    Transition,
)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_search_issues(
    jql: str,
    max_results: int = 20,
) -> list[JiraIssue]:
    """Search Jira issues using JQL.

    Args:
        jql: JQL query string, e.g. ``"project = XYZ AND status = Open"``.
        max_results: Maximum number of issues to return (default 20).

    Returns:
        List of JiraIssue models with: key, summary, status, assignee,
        priority, issue_type, project, labels, created, updated, url.
    """
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().search_issues(jql, max_results)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_issue(issue_key: str) -> JiraIssue:
    """Fetch a single Jira issue by key.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.

    Returns:
        JiraIssue with key, summary, status, assignee, priority, url, etc.
    """
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().get_issue(issue_key)


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def jira_create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Story",
    priority: str = "Medium",
    labels: list[str] | None = None,
) -> CreatedIssue:
    """Create a new Jira issue.

    Args:
        project_key: Project key, e.g. ``"XYZ"``.
        summary: Issue title / summary.
        description: Longer description (plain text).
        issue_type: ``"Story"``, ``"Bug"``, ``"Task"``, ``"Epic"``, etc.
        priority: ``"Highest"``, ``"High"``, ``"Medium"``, ``"Low"``.
        labels: Optional list of label strings.

    Returns:
        CreatedIssue with key, id, and browse url.
    """
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().create_issue(
        project_key, summary, description, issue_type, priority, labels
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def jira_update_issue(
    issue_key: str,
    fields: dict,
) -> JiraIssue:
    """Update fields on an existing Jira issue.

    Args:
        issue_key: Issue key, e.g. ``"PROJ-123"``.
        fields: Dict of Jira field names to new values,
                e.g. ``{"priority": {"name": "High"}}``.

    Returns:
        Updated JiraIssue.
    """
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().update_issue(issue_key, fields)


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
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
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().add_comment(issue_key, comment)


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
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().get_transitions(issue_key)


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
    from workflow_builder.toolsets.jira.client import get_default_client

    client = get_default_client()
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
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().list_projects()


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def jira_get_myself() -> JiraUser:
    """Get the authenticated Jira user's profile.

    Returns:
        JiraUser with account_id, display_name, and email.
    """
    from workflow_builder.toolsets.jira.client import get_default_client

    return await get_default_client().get_myself()


# ---------------------------------------------------------------------------
# Auto-generated tool documentation
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    """Build JIRA_TOOL_DOCS from model schemas and function signatures.

    This keeps the docs DRY — they are derived from the actual
    Pydantic models and function signatures, not hand-written.
    """
    from workflow_builder.toolsets.jira.models import (
        Comment as _Comment,
    )
    from workflow_builder.toolsets.jira.models import (
        CreatedIssue as _CreatedIssue,
    )
    from workflow_builder.toolsets.jira.models import (
        JiraIssue as _JiraIssue,
    )
    from workflow_builder.toolsets.jira.models import (
        JiraProject as _JiraProject,
    )
    from workflow_builder.toolsets.jira.models import (
        JiraUser as _JiraUser,
    )
    from workflow_builder.toolsets.jira.models import (
        Transition as _Transition,
    )

    def _fields(model: type) -> str:
        props = model.model_json_schema().get("properties", {})
        return ", ".join(props)

    return f"""\
## Available Jira Tools

Import: from workflow_builder.toolsets.jira.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN

All tools return typed Pydantic models (not plain dicts).
Use attribute access: issue.key, issue.status, project.name, etc.

### Tools

jira_search_issues(jql: str, max_results: int = 20) -> list[JiraIssue]
  Search using a JQL string.
  JiraIssue fields: {_fields(_JiraIssue)}
  Examples:
    issues = await ctx.step(jira_search_issues, \
"project = XYZ AND status = \\"To Do\\"")
    issues = await ctx.step(jira_search_issues, \
"assignee = currentUser() AND sprint in openSprints()")
    issues = await ctx.step(jira_search_issues, \
"issuetype = Bug AND priority = High", 50)

jira_get_issue(issue_key: str) -> JiraIssue
  Fetch a single issue by key.
    issue = await ctx.step(jira_get_issue, "PROJ-123")
    print(issue.summary, issue.status)

jira_create_issue(project_key, summary, description="", \
issue_type="Story", priority="Medium", labels=None) -> CreatedIssue
  Create an issue.
  CreatedIssue fields: {_fields(_CreatedIssue)}
    created = await ctx.step(jira_create_issue, "XYZ", "Add login", \
"SSO support", "Story", "High")
    print(created.key, created.url)

jira_update_issue(issue_key: str, fields: dict) -> JiraIssue
  Update Jira fields. Returns updated issue.
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

jira_get_myself() -> JiraUser
  Get authenticated user.
  JiraUser fields: {_fields(_JiraUser)}
    me = await ctx.step(jira_get_myself)
    print(me.display_name, me.email)
"""


JIRA_TOOL_DOCS: str = _build_tool_docs()
