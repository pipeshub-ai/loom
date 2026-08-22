"""Asana step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.asana.tools import asana_list_tasks, asana_create_task

    tasks = await ctx.step(asana_list_tasks, project_gid="1201234567890")
    made  = await ctx.step(asana_create_task, title="Fix login", projects=["120…"])

Retries are set per operation. Reads retry; **creating a task and adding a
comment do not**, because Asana offers no idempotency key: a timeout after it
accepted the write is indistinguishable from a failure, so a retry files the
task twice. Journaling covers replay; it does not cover the attempt.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.asana.client import AsanaClient
from loom.toolsets.asana.models import (
    AsanaProject,
    AsanaSection,
    AsanaStory,
    AsanaTask,
    AsanaUser,
    AsanaWorkspace,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
#: Once. A PUT or DELETE naming the same task twice reaches the same end state.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
#: None. See the module docstring.
_UNSAFE_WRITE = Retry(max_attempts=1)


# -- identity and structure -------------------------------------------------


@step(retry=_READ)
async def asana_whoami() -> AsanaUser:
    """Return the user this token authenticates as.

    Returns:
        The authenticated user's gid, name, and email.
    """

    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).whoami()


@step(retry=_READ)
async def asana_list_workspaces() -> list[AsanaWorkspace]:
    """List the workspaces this token can see.

    The entry point: search, project listing, and user lookup all need a
    workspace gid, and none of them accept a workspace name.

    Returns:
        Workspaces with gid, name, and whether each is an organization.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).list_workspaces()


@step(retry=_READ)
async def asana_list_projects(
    workspace_gid: str, limit: int = 50, archived: bool = False
) -> Results[AsanaProject]:
    """List the projects in a workspace.

    Args:
        workspace_gid: Workspace gid, from ``asana_list_workspaces``.
        limit: Maximum projects across all pages. Defaults to 50.
        archived: Include archived projects. Defaults to False.

    Returns:
        Paginated projects. Check ``.complete`` before reporting a total.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).list_projects(
        workspace_gid, limit=limit, archived=archived
    )


@step(retry=_READ)
async def asana_list_sections(project_gid: str) -> list[AsanaSection]:
    """List the sections in a project.

    Args:
        project_gid: Project gid, from ``asana_list_projects``.

    Returns:
        Sections with gid and name, in board order.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).list_sections(project_gid)


# -- tasks ------------------------------------------------------------------


@step(retry=_READ)
async def asana_list_tasks(
    project_gid: str, limit: int = 50, completed_since: str = ""
) -> Results[AsanaTask]:
    """List the tasks in a project.

    The dependable way to read tasks: unlike search, this pages and is
    available on every plan.

    Args:
        project_gid: Project gid, from ``asana_list_projects``.
        limit: Maximum tasks across all pages. Defaults to 50.
        completed_since: ISO-8601 instant, or "now" for incomplete tasks only.

    Returns:
        Paginated tasks with assignee, due date, projects, and URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).list_tasks(
        project_gid, limit=limit, completed_since=completed_since
    )


@step(retry=_READ)
async def asana_search_tasks(
    workspace_gid: str,
    text: str = "",
    assignee_gid: str = "",
    project_gids: list[str] | None = None,
    completed: bool | None = None,
    limit: int = 50,
) -> list[AsanaTask]:
    """Full-text search for tasks across a workspace.

    Two limits worth knowing before choosing this over ``asana_list_tasks``.
    It is **premium-only** — a free workspace raises ``AsanaPremiumRequired``
    — and it **does not paginate**, because Asana states results are unstable
    across identical queries. It returns a plain list for that reason: there
    is no page to follow and no coverage to report.

    ``assignee_gid`` takes a gid, not a name. Resolve one with
    ``asana_find_users`` first, or the filter matches nothing and reports no
    error.

    Args:
        workspace_gid: Workspace to search within.
        text: Free text matched against task name and notes.
        assignee_gid: Restrict to this assignee's gid.
        project_gids: Restrict to these projects.
        completed: True for completed only, False for incomplete only.
        limit: Maximum tasks, capped at 100 by Asana.

    Returns:
        Matching tasks, most recently modified first.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).search_tasks(
        workspace_gid,
        text=text,
        assignee_gid=assignee_gid,
        project_gids=project_gids,
        completed=completed,
        limit=limit,
    )


@step(retry=_READ)
async def asana_get_task(task_gid: str) -> AsanaTask:
    """Fetch one task by gid.

    Args:
        task_gid: Asana task gid.

    Returns:
        The task, with assignee, due date, projects, tags, and URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).get_task(task_gid)


@step(retry=_UNSAFE_WRITE)
async def asana_create_task(
    title: str,
    workspace_gid: str = "",
    notes: str = "",
    projects: list[str] | None = None,
    assignee_gid: str = "",
    due_on: str = "",
    parent: str = "",
) -> AsanaTask:
    """Create a task.

    A task needs a home: pass ``projects``, a ``parent``, or a
    ``workspace_gid``. Sending none is a 400 that names no field.

    Not retried: Asana has no idempotency key, so a timeout after the task was
    filed would file a second one.

    Args:
        title: Task title.
        workspace_gid: Workspace to create in, when no project or parent is given.
        notes: Task body. Plain text.
        projects: Project gids to add the task to.
        assignee_gid: User gid to assign, from ``asana_find_users``.
        due_on: Due date as ``YYYY-MM-DD``.
        parent: Task gid to create this as a subtask of.

    Returns:
        The created task, including its gid and permalink URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).create_task(
        workspace_gid,
        name=title,
        notes=notes,
        projects=projects,
        assignee_gid=assignee_gid,
        due_on=due_on,
        parent=parent,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def asana_update_task(
    task_gid: str,
    title: str = "",
    notes: str = "",
    completed: bool | None = None,
    assignee_gid: str = "",
    due_on: str = "",
) -> AsanaTask:
    """Update a task. Only the fields you pass are changed.

    Args:
        task_gid: Task to update.
        title: New title.
        notes: New body.
        completed: True to complete the task, False to reopen it.
        assignee_gid: User gid to assign.
        due_on: Due date as ``YYYY-MM-DD``.

    Returns:
        The updated task.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).update_task(
        task_gid,
        name=title,
        notes=notes,
        completed=completed,
        assignee_gid=assignee_gid,
        due_on=due_on,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def asana_complete_task(task_gid: str) -> AsanaTask:
    """Mark a task complete.

    The single most common Asana write, so it is its own operation rather than
    something a caller has to know is ``completed=True`` on an update.

    Args:
        task_gid: Task to complete.

    Returns:
        The completed task.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).update_task(task_gid, completed=True)


@step(retry=_IDEMPOTENT_WRITE)
async def asana_delete_task(task_gid: str) -> bool:
    """Delete a task.

    Args:
        task_gid: Task to delete.

    Returns:
        True once Asana has accepted the deletion.
    """
    from loom.toolsets.factory import client_for

    await (await client_for("asana", AsanaClient)).delete_task(task_gid)
    return True


# -- comments ---------------------------------------------------------------


@step(retry=_READ)
async def asana_list_comments(task_gid: str, limit: int = 50) -> list[AsanaStory]:
    """List the comments on a task.

    Filtered from Asana's story feed, which also records every field change —
    "show me the comments" almost never means "show me that someone edited the
    due date in March".

    Args:
        task_gid: Task whose comments to read.
        limit: Maximum stories to scan, capped at 100 by Asana.

    Returns:
        Comment stories with text, author, and timestamp.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).list_comments(task_gid, limit=limit)


@step(retry=_UNSAFE_WRITE)
async def asana_add_comment(task_gid: str, text: str) -> AsanaStory:
    """Add a comment to a task.

    Not retried: there is no idempotency key, so a retry after a timeout
    comments twice on a task a person is reading.

    Args:
        task_gid: Task to comment on.
        text: Comment body. Plain text.

    Returns:
        The created comment.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("asana", AsanaClient)).add_comment(task_gid, text)


# -- people -----------------------------------------------------------------


@step(retry=_READ)
async def asana_find_users(
    workspace_gid: str, query: str = "", count: int = 20
) -> list[AsanaUser]:
    """Find people in a workspace by name.

    Resolve a person here before assigning or filtering: every Asana write
    takes a user gid, and a name passed where a gid belongs matches nothing and
    reports no error. Results are ranked most-contacted first, which is usually
    what someone means by "assign it to Priya".

    Args:
        workspace_gid: Workspace to search within.
        query: Name to search for. Empty returns frequent collaborators.
        count: Maximum results, capped at 100 by Asana.

    Returns:
        Users with gid, name, and email.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("asana", AsanaClient)
    return await client.find_users(workspace_gid, query, count=count)
