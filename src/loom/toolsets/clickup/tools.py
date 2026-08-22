"""ClickUp step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.clickup.tools import clickup_list_tasks, clickup_create_task

    tasks = await ctx.step(clickup_list_tasks, list_id="901234567")
    made  = await ctx.step(clickup_create_task, list_id="901234567", title="Fix login")

Retries are set per operation rather than uniformly. Reads retry; **creating a
task and posting a comment do not**, because neither has an idempotency key: a
timeout after ClickUp accepted the write is indistinguishable from a failure, so
retrying posts the comment twice. Journaling covers replay; it does not cover
the attempt.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.clickup.client import ClickUpClient
from loom.toolsets.clickup.models import (
    ClickUpComment,
    ClickUpContainer,
    ClickUpTask,
    ClickUpUser,
    ClickUpWorkspace,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
#: Once. A PUT or DELETE naming the same task twice is the same end state, so
#: one retry is safe where a create is not.
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
#: None. See the module docstring.
_UNSAFE_WRITE = Retry(max_attempts=1)


# -- workspace navigation ---------------------------------------------------


@step(retry=_READ)
async def clickup_list_workspaces() -> list[ClickUpWorkspace]:
    """List the ClickUp workspaces this token can see.

    The entry point: almost every other ClickUp call needs a workspace, space,
    folder, or list id, and none of them are guessable from a name.

    Returns:
        Workspaces, each with id and name.
    """

    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).list_workspaces()


@step(retry=_READ)
async def clickup_list_spaces(
    workspace_id: str, archived: bool = False
) -> list[ClickUpContainer]:
    """List the spaces in a workspace.

    Args:
        workspace_id: Workspace id, from ``clickup_list_workspaces``.
        archived: Include archived spaces. Defaults to False.

    Returns:
        Containers with kind="space".
    """
    from loom.toolsets.factory import client_for

    client = await client_for("clickup", ClickUpClient)
    return await client.list_spaces(workspace_id, archived=archived)


@step(retry=_READ)
async def clickup_list_folders(
    space_id: str, archived: bool = False
) -> list[ClickUpContainer]:
    """List the folders in a space.

    Args:
        space_id: Space id, from ``clickup_list_spaces``.
        archived: Include archived folders. Defaults to False.

    Returns:
        Containers with kind="folder".
    """
    from loom.toolsets.factory import client_for

    client = await client_for("clickup", ClickUpClient)
    return await client.list_folders(space_id, archived=archived)


@step(retry=_READ)
async def clickup_list_lists(
    space_id: str = "", folder_id: str = "", archived: bool = False
) -> list[ClickUpContainer]:
    """List the task lists in a folder, or the folderless lists in a space.

    Pass exactly one of the two ids. A list may sit inside a folder or directly
    in a space, and the folderless ones are invisible through the folder route.

    Args:
        space_id: Space id, for lists that sit directly in a space.
        folder_id: Folder id, for lists inside a folder.
        archived: Include archived lists. Defaults to False.

    Returns:
        Containers with kind="list".
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).list_lists(
        space_id=space_id, folder_id=folder_id, archived=archived
    )


# -- tasks ------------------------------------------------------------------


@step(retry=_READ)
async def clickup_list_tasks(
    list_id: str,
    limit: int = 50,
    include_closed: bool = False,
    subtasks: bool = False,
    statuses: list[str] | None = None,
    assignees: list[str] | None = None,
) -> Results[ClickUpTask]:
    """List tasks in a ClickUp list, newest first.

    Args:
        list_id: List id, from ``clickup_list_lists``.
        limit: Maximum tasks to return across all pages. Defaults to 50.
        include_closed: Include closed tasks. Defaults to False.
        subtasks: Include subtasks. Defaults to False.
        statuses: Status names to filter by, e.g. ``["in progress"]``.
        assignees: Numeric user ids, from ``clickup_find_members``.

    Returns:
        Paginated tasks. Check ``.complete`` before reporting a total.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).list_tasks(
        list_id,
        limit=limit,
        include_closed=include_closed,
        subtasks=subtasks,
        statuses=statuses,
        assignees=assignees,
    )


@step(retry=_READ)
async def clickup_search_tasks(
    workspace_id: str,
    limit: int = 50,
    space_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    statuses: list[str] | None = None,
    assignees: list[str] | None = None,
    include_closed: bool = False,
) -> Results[ClickUpTask]:
    """Find tasks across a whole workspace, filtered.

    The workspace-wide view, where ``clickup_list_tasks`` is scoped to one
    list. ``assignees`` takes numeric user ids — resolve a name with
    ``clickup_find_members`` first, or the filter matches nothing and returns
    no error.

    Args:
        workspace_id: Workspace id, from ``clickup_list_workspaces``.
        limit: Maximum tasks across all pages. Defaults to 50.
        space_ids: Restrict to these spaces.
        list_ids: Restrict to these lists.
        statuses: Status names to filter by.
        assignees: Numeric user ids.
        include_closed: Include closed tasks. Defaults to False.

    Returns:
        Paginated tasks.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).search_tasks(
        workspace_id,
        limit=limit,
        space_ids=space_ids,
        list_ids=list_ids,
        statuses=statuses,
        assignees=assignees,
        include_closed=include_closed,
    )


@step(retry=_READ)
async def clickup_get_task(task_id: str) -> ClickUpTask:
    """Fetch one task by id.

    Args:
        task_id: ClickUp task id, e.g. ``"86a1b2c3d"``.

    Returns:
        The task, with status, assignees, dates, tags, and URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).get_task(task_id)


@step(retry=_UNSAFE_WRITE)
async def clickup_create_task(
    list_id: str,
    title: str,
    description: str = "",
    assignees: list[str] | None = None,
    status: str = "",
    priority: int | None = None,
    due_date: int | None = None,
    tags: list[str] | None = None,
    parent: str = "",
) -> ClickUpTask:
    """Create a task in a list.

    Not retried: ClickUp offers no idempotency key, so a timeout after the task
    was created would create a second one.

    Args:
        list_id: List to create the task in.
        title: Task title.
        description: Task body. Plain text.
        assignees: Numeric user ids to assign.
        status: Status name. Defaults to the list's first status.
        priority: 1 (urgent) to 4 (low).
        due_date: Unix milliseconds.
        tags: Tag names to apply.
        parent: Task id to create this as a subtask of.

    Returns:
        The created task, including its id and URL.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).create_task(
        list_id,
        title,
        description=description,
        assignees=assignees,
        status=status,
        priority=priority,
        due_date=due_date,
        tags=tags,
        parent=parent,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def clickup_update_task(
    task_id: str,
    title: str = "",
    description: str = "",
    status: str = "",
    priority: int | None = None,
    due_date: int | None = None,
    archived: bool | None = None,
) -> ClickUpTask:
    """Update a task. Only the fields you pass are changed.

    Args:
        task_id: Task to update.
        title: New title.
        description: New body.
        status: New status name, e.g. ``"complete"``.
        priority: 1 (urgent) to 4 (low).
        due_date: Unix milliseconds.
        archived: Archive or unarchive.

    Returns:
        The updated task.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).update_task(
        task_id,
        name=title,
        description=description,
        status=status,
        priority=priority,
        due_date=due_date,
        archived=archived,
    )


@step(retry=_IDEMPOTENT_WRITE)
async def clickup_delete_task(task_id: str) -> bool:
    """Delete a task permanently.

    Args:
        task_id: Task to delete.

    Returns:
        True once ClickUp has accepted the deletion.
    """
    from loom.toolsets.factory import client_for

    await (await client_for("clickup", ClickUpClient)).delete_task(task_id)
    return True


# -- comments ---------------------------------------------------------------


@step(retry=_READ)
async def clickup_list_comments(task_id: str) -> list[ClickUpComment]:
    """List the comments on a task, newest first.

    Args:
        task_id: Task whose comments to read.

    Returns:
        Comments with text, author, date, and resolved flag.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).list_comments(task_id)


@step(retry=_UNSAFE_WRITE)
async def clickup_create_comment(
    task_id: str, text: str, assignee: str = "", notify_all: bool = False
) -> ClickUpComment:
    """Post a comment on a task.

    Not retried: there is no idempotency key, so a retry after a timeout
    comments twice on a task a person is reading.

    Args:
        task_id: Task to comment on.
        text: Comment body. Plain text.
        assignee: Numeric user id to assign the comment to.
        notify_all: Notify everyone watching the task. Defaults to False.

    Returns:
        The created comment.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).create_comment(
        task_id, text, assignee=assignee, notify_all=notify_all
    )


# -- people -----------------------------------------------------------------


@step(retry=_READ)
async def clickup_find_members(workspace_id: str, query: str = "") -> list[ClickUpUser]:
    """Find workspace members by name or email.

    Resolve a person here before filtering or assigning: every ClickUp write
    takes a numeric user id, and a name passed where an id belongs matches
    nothing and reports no error.

    Args:
        workspace_id: Workspace to search within.
        query: Name or email substring. Empty returns the whole roster.

    Returns:
        Users with id, username, and email.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).find_members(workspace_id, query)


@step(retry=_READ)
async def clickup_whoami() -> ClickUpUser:
    """Return the user this token authenticates as.

    Returns:
        The authenticated user's id, username, and email.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("clickup", ClickUpClient)).whoami()
