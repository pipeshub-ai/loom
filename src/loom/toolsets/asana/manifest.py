"""Asana ToolsetManifest — pure metadata, no client import.

Output schemas come from the Pydantic models so the contract cannot drift from
what the tools return. Note which operations declare ``pagination=True``: the
project and task *listings* do, and ``tasks.search`` does not, because Asana's
search endpoint has no offset to follow. Declaring it there would promise a
completeness guarantee the API cannot keep, and
``tests/test_manifest_imports.py`` checks that claim three ways.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.asana.models import (
    AsanaProject,
    AsanaSection,
    AsanaStory,
    AsanaTask,
    AsanaUser,
    AsanaWorkspace,
)
from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


ASANA_MANIFEST = ToolsetManifest(
    id="asana",
    version="1.0.0",
    summary="Asana — tasks, projects, comments, and the people assigned to them.",
    description=(
        "Asana API 1.0. List and search tasks, create/update/complete/delete "
        "them, read and post comments, walk workspaces → projects → sections, "
        "and resolve a person's name to the gid every assignment requires."
    ),
    base_url="https://app.asana.com/api/1.0",
    auth=AuthSpec(
        client="loom.toolsets.asana.client:AsanaClient",
        # This client reads environment variables and no CredentialStore, so
        # `credential` is empty and no `provider` is declared: an OAuth flow
        # here would store a token the client never looks up. Adding a store
        # path is a change to the client, not to this manifest.
        kind="bearer",
        fields=(
            AuthField(name="ASANA_ACCESS_TOKEN", arg="access_token", label="Personal access token"),
        ),
    ),
    tools_module="loom.toolsets.asana.tools",
    egress_hosts=["app.asana.com"],
    rate_limits={
        "model": "per-minute windows, tiered by whether the domain is free or paid",
        "search": "60 requests per minute",
        "concurrency": (
            "duplication, instantiation and export endpoints allow 5 "
            "concurrent jobs per user"
        ),
        "source": "developers.asana.com/docs/rate-limits",
    },
    groups={
        "structure": [
            OperationSpec(
                id="structure.list_workspaces",
                function="asana_list_workspaces",
                summary="List the workspaces this token can see.",
                description=(
                    "The entry point — search, project listing, and user "
                    "lookup all need a workspace gid."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(AsanaWorkspace),
            ),
            OperationSpec(
                id="structure.list_projects",
                function="asana_list_projects",
                summary="List the projects in a workspace.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(AsanaProject),
            ),
            OperationSpec(
                id="structure.list_sections",
                function="asana_list_sections",
                summary="List the sections in a project.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(AsanaSection),
            ),
        ],
        "tasks": [
            OperationSpec(
                id="tasks.list",
                function="asana_list_tasks",
                summary="List the tasks in a project.",
                description=(
                    "The dependable read: it pages and is available on every "
                    "plan, unlike search."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(AsanaTask),
            ),
            OperationSpec(
                id="tasks.search",
                function="asana_search_tasks",
                summary="Full-text search for tasks across a workspace.",
                description=(
                    "Premium-only, and does not paginate — Asana states "
                    "results are unstable across identical queries. Prefer "
                    "tasks.list where a project is known. assignee_gid takes a "
                    "gid, so resolve a name with people.find_users first."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                # Deliberately False: there is no offset to follow.
                pagination=False,
                output_schema=_array(AsanaTask),
            ),
            OperationSpec(
                id="tasks.get",
                function="asana_get_task",
                summary="Fetch one task by gid.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=AsanaTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.create",
                function="asana_create_task",
                summary="Create a task in a project, under a parent, or in a workspace.",
                description="Not retried: Asana has no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=AsanaTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.update",
                function="asana_update_task",
                summary="Update a task. Only the fields passed are changed.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=AsanaTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.complete",
                function="asana_complete_task",
                summary="Mark a task complete.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=AsanaTask.model_json_schema(),
            ),
            OperationSpec(
                id="tasks.delete",
                function="asana_delete_task",
                summary="Delete a task.",
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
        "comments": [
            OperationSpec(
                id="comments.list",
                function="asana_list_comments",
                summary="List the comments on a task.",
                description=(
                    "Filtered from the story feed, which also records every "
                    "field change."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(AsanaStory),
            ),
            OperationSpec(
                id="comments.add",
                function="asana_add_comment",
                summary="Add a comment to a task.",
                description="Not retried: no idempotency key, so a retry double-posts.",
                effect=EffectClass.WRITE,
                output_schema=AsanaStory.model_json_schema(),
            ),
        ],
        "people": [
            OperationSpec(
                id="people.find_users",
                function="asana_find_users",
                summary="Find people in a workspace by name.",
                description=(
                    "The join between a person's name and the gid every "
                    "assignment needs. Ranked most-contacted first."
                ),
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(AsanaUser),
            ),
            OperationSpec(
                id="people.whoami",
                function="asana_whoami",
                summary="The user this token authenticates as.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=AsanaUser.model_json_schema(),
            ),
        ],
    },
)
