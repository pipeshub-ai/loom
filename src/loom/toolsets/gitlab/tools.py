"""GitLab step functions for use inside LOOM workflows.

    from loom.toolsets.gitlab.tools import gitlab_list_issues

    issues = await ctx.step(gitlab_list_issues, project="group/app", state="opened")

Every issue and merge request path takes an **iid** — the per-project number in
the URL — not the global ``id``. Both are on the returned models, named as
GitLab names them, because passing one where the other belongs addresses a
different record and reports no error.

Retries are per operation. Reads retry; **creating an issue, a note, or a merge
request does not**, because GitLab has no idempotency key and a retry after a
timeout files a second one.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.gitlab.models import (
    GitLabIssue,
    GitLabMergeRequest,
    GitLabNote,
    GitLabProject,
    GitLabUser,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


@step(retry=_READ)
async def gitlab_whoami() -> GitLabUser:
    """Return the user this token authenticates as.

    Returns:
        The authenticated user's id, username, name, and profile URL.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().whoami()


@step(retry=_READ)
async def gitlab_find_users(query: str, limit: int = 20) -> list[GitLabUser]:
    """Find people by username, name, or email.

    Resolve someone here before assigning: GitLab assignments take numeric
    ``assignee_ids``, and a username passed where an id belongs is ignored.

    Args:
        query: Username, name, or email to search for.
        limit: Maximum users. Defaults to 20.

    Returns:
        Users with id, username, name, and profile URL.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().find_users(query, limit=limit)


@step(retry=_READ)
async def gitlab_list_projects(
    search: str = "",
    membership: bool = True,
    limit: int = 50,
    order_by: str = "last_activity_at",
) -> Results[GitLabProject]:
    """List projects, most recently active first.

    Resolve a project here before working in it: paths and ids are both
    accepted downstream, but a project *name* is neither.

    Args:
        search: Substring of the project name or path.
        membership: Only projects you are a member of. Defaults to True.
        limit: Maximum projects across all pages. Defaults to 50.
        order_by: ``last_activity_at``, ``created_at``, ``name``, or ``id``.

    Returns:
        Paginated projects. Check ``.complete`` before reporting a total.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().list_projects(
        search=search, membership=membership, limit=limit, order_by=order_by
    )


@step(retry=_READ)
async def gitlab_get_project(project: str) -> GitLabProject:
    """Fetch one project by numeric id or by ``group/project`` path.

    Args:
        project: ``"12345"`` or ``"group/app"``. The path is URL-encoded for
            you — an unencoded slash returns 404, which reads as a missing
            project rather than a missing escape.

    Returns:
        The project, with default branch, visibility, and counts.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().get_project(project)


@step(retry=_READ)
async def gitlab_list_issues(
    project: str,
    state: str = "opened",
    labels: str = "",
    assignee: str = "",
    limit: int = 50,
) -> Results[GitLabIssue]:
    """List issues in a project.

    Args:
        project: Numeric id or ``group/project``.
        state: ``opened``, ``closed``, or ``all``. Note **opened**, not "open"
            — GitLab ignores an unknown state and returns everything.
        labels: Comma-separated label names.
        assignee: A username to filter by.
        limit: Maximum issues across all pages. Defaults to 50.

    Returns:
        Paginated issues, each with its ``iid``.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().list_issues(
        project, state=state, labels=labels, assignee=assignee, limit=limit
    )


@step(retry=_READ)
async def gitlab_get_issue(project: str, iid: int) -> GitLabIssue:
    """Fetch one issue by its per-project number.

    Args:
        project: Numeric id or ``group/project``.
        iid: The number in the issue's URL. Not the global ``id``.

    Returns:
        The issue.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().get_issue(project, iid)


@step(retry=_UNSAFE_WRITE)
async def gitlab_create_issue(
    project: str,
    title: str,
    description: str = "",
    labels: list[str] | None = None,
    assignee_ids: list[str] | None = None,
) -> GitLabIssue:
    """Open an issue.

    Not retried: GitLab has no idempotency key, so a timeout after the issue
    was filed would file a second one.

    Args:
        project: Numeric id or ``group/project``.
        title: Issue title.
        description: Issue body. Markdown.
        labels: Label names to apply.
        assignee_ids: Numeric user ids, from ``gitlab_find_users``.

    Returns:
        The created issue, including its ``iid`` and URL.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().create_issue(
        project,
        title,
        description=description,
        labels=labels,
        assignee_ids=assignee_ids,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def gitlab_update_issue(
    project: str,
    iid: int,
    title: str = "",
    description: str = "",
    state_event: str = "",
    labels: list[str] | None = None,
) -> GitLabIssue:
    """Update an issue — retitle, edit, relabel, close, or reopen.

    Args:
        project: Numeric id or ``group/project``.
        iid: The issue's per-project number.
        title: New title.
        description: New body.
        state_event: ``close`` or ``reopen``. GitLab takes the *transition*,
            not a state — sending ``"closed"`` here changes nothing, silently.
        labels: Replace the labels with these.

    Returns:
        The updated issue.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().update_issue(
        project,
        iid,
        title=title,
        description=description,
        state_event=state_event,
        labels=labels,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def gitlab_close_issue(project: str, iid: int) -> GitLabIssue:
    """Close an issue.

    Its own operation because it is the most common issue write and because
    the parameter that does it is easy to get wrong: it is ``state_event="close"``,
    not a state.

    Args:
        project: Numeric id or ``group/project``.
        iid: The issue's per-project number.

    Returns:
        The closed issue.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().update_issue(project, iid, state_event="close")


@step(retry=_READ)
async def gitlab_list_issue_notes(
    project: str, iid: int, limit: int = 50, include_system: bool = False
) -> Results[GitLabNote]:
    """List the notes on an issue. System records are excluded by default.

    GitLab records "changed the milestone" as a note as well, and "show me the
    comments" does not mean that.

    Args:
        project: Numeric id or ``group/project``.
        iid: The issue's per-project number.
        limit: Maximum notes across all pages. Defaults to 50.
        include_system: Keep GitLab's own system notes.

    Returns:
        Paginated notes with body, author, and timestamp.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().list_issue_notes(
        project, iid, limit=limit, include_system=include_system
    )


@step(retry=_UNSAFE_WRITE)
async def gitlab_add_issue_note(project: str, iid: int, body: str) -> GitLabNote:
    """Comment on an issue.

    Not retried: no idempotency key, so a retry comments twice on a thread
    people are reading.

    Args:
        project: Numeric id or ``group/project``.
        iid: The issue's per-project number.
        body: Note body. Markdown.

    Returns:
        The created note.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().add_issue_note(project, iid, body)


@step(retry=_READ)
async def gitlab_list_merge_requests(
    project: str, state: str = "opened", target_branch: str = "", limit: int = 50
) -> Results[GitLabMergeRequest]:
    """List merge requests in a project.

    Args:
        project: Numeric id or ``group/project``.
        state: ``opened``, ``closed``, ``merged``, or ``all``.
        target_branch: Only merge requests targeting this branch.
        limit: Maximum merge requests across all pages. Defaults to 50.

    Returns:
        Paginated merge requests with branches, draft flag, and merge status.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().list_merge_requests(
        project, state=state, target_branch=target_branch, limit=limit
    )


@step(retry=_READ)
async def gitlab_get_merge_request(project: str, iid: int) -> GitLabMergeRequest:
    """Fetch one merge request by its per-project number.

    Args:
        project: Numeric id or ``group/project``.
        iid: The number in the merge request's URL.

    Returns:
        The merge request.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().get_merge_request(project, iid)


@step(retry=_UNSAFE_WRITE)
async def gitlab_create_merge_request(
    project: str,
    title: str,
    source_branch: str,
    target_branch: str,
    description: str = "",
    draft: bool = False,
) -> GitLabMergeRequest:
    """Open a merge request.

    Not retried: a retry after a timeout opens a second one for the same
    branch pair.

    Args:
        project: Numeric id or ``group/project``.
        title: Merge request title.
        source_branch: Branch to merge from.
        target_branch: Branch to merge into.
        description: Description. Markdown.
        draft: Open as a draft. GitLab marks this with a title prefix.

    Returns:
        The created merge request.
    """
    from loom.toolsets.gitlab.client import get_default_client

    return await get_default_client().create_merge_request(
        project,
        title,
        source_branch=source_branch,
        target_branch=target_branch,
        description=description,
        draft=draft,
    )
