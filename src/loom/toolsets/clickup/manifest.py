"""ClickUp ToolsetManifest — pure metadata, no client import.

Output schemas come from the Pydantic models so the contract cannot drift from
what the tools actually return. Nothing here imports ``client`` or ``httpx``:
the catalog reads every manifest at registration, and a manifest that dragged
in a transport would undo the lazy layering it is registered into.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.clickup.models import (
    ClickUpComment,
    ClickUpContainer,
    ClickUpTask,
    ClickUpUser,
    ClickUpWorkspace,
)
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


CLICKUP_MANIFEST = ToolsetManifest(
    id="clickup",
    version="1.0.0",
    summary="ClickUp — tasks, comments, and the workspace tree above them.",
    description=(
        "ClickUp API v2. Navigate workspaces → spaces → folders → lists, then "
        "list, search, create, update, and delete tasks; read and post "
        "comments; and resolve a person's name to the numeric user id every "
        "write requires."
    ),
    base_url="https://api.clickup.com/api/v2",
    auth={
        # Two shapes, sent differently: a personal token goes in Authorization
        # raw, an OAuth token takes a Bearer prefix.
        "type": "token",
        "fields": ["CLICKUP_API_TOKEN", "CLICKUP_OAUTH_TOKEN"],
    },
    tools_module="loom.toolsets.clickup.tools",
    egress_hosts=["api.clickup.com"],
    rate_limits={
        "model": "per minute, per token, tiered by plan",
        "free_unlimited_business": "100 requests per minute",
        "business_plus": "1,000 requests per minute",
        "enterprise": "10,000 requests per minute",
        "source": "developer.clickup.com/docs/rate-limits",
    },
    groups={
        "workspace": [
            OperationSpec(
                id="workspace.list_workspaces",
                function="clickup_list_workspaces",
                summary="List the workspaces this token can see.",
                description=(
                    "The entry point — every other call needs an id that is "
                    "not guessable from a name."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(ClickUpWorkspace),
            ),
            OperationSpec(
                id="workspace.list_spaces",
                function="clickup_list_spaces",
                summary="List the spaces in a workspace.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(ClickUpContainer),
            ),
            OperationSpec(
                id="workspace.list_folders",
                function="clickup_list_folders",
                summary="List the folders in a space.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(ClickUpContainer),
            ),
            OperationSpec(
                id="workspace.list_lists",
                function="clickup_list_lists",
                summary="List task lists in a folder, or folderless lists in a space.",
                description=(
                    "Pass exactly one of space_id or folder_id. Folderless "
                    "lists are invisible through the folder route."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(ClickUpContainer),
            ),
        ],
        "tasks": [
            OperationSpec(
                id="tasks.list",
                function="clickup_list_tasks",
                summary="List tasks in a list.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ClickUpTask),
            ),
            OperationSpec(
                id="tasks.search",
                function="clickup_search_tasks",
                summary="Find tasks across a workspace, filtered.",
                description=(
                    "The assignees filter takes numeric user ids — resolve a "
                    "name with clickup_find_members first, or it matches "
                    "nothing and reports no error."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(ClickUpTask),
            ),
            OperationSpec(
                id="tasks.get",
                function="clickup_get_task",
                summary="Fetch one task by id.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=ClickUpTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.create",
                function="clickup_create_task",
                summary="Create a task in a list.",
                description="Not retried: ClickUp has no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=ClickUpTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.update",
                function="clickup_update_task",
                summary="Update a task. Only the fields passed are changed.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=ClickUpTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.delete",
                function="clickup_delete_task",
                summary="Delete a task permanently.",
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
        "comments": [
            OperationSpec(
                id="comments.list",
                function="clickup_list_comments",
                summary="List the comments on a task.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(ClickUpComment),
            ),
            OperationSpec(
                id="comments.create",
                function="clickup_create_comment",
                summary="Post a comment on a task.",
                description="Not retried: no idempotency key, so a retry double-posts.",
                effect=EffectClass.WRITE,
                output_schema=ClickUpComment.model_json_schema(),
            ),
        ],
        "people": [
            OperationSpec(
                id="people.find_members",
                function="clickup_find_members",
                summary="Find workspace members by name or email.",
                description=(
                    "The join between what a person is called and the numeric "
                    "id every write wants."
                ),
                # What tells the coding agent to resolve before filtering,
                # without it knowing anything about ClickUp.
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(ClickUpUser),
            ),
            OperationSpec(
                id="people.whoami",
                function="clickup_whoami",
                summary="The user this token authenticates as.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=ClickUpUser.model_json_schema(),
            ),
        ],
    },
)
