"""GitLab ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.gitlab.models import (
    GitLabIssue,
    GitLabMergeRequest,
    GitLabNote,
    GitLabProject,
    GitLabUser,
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


GITLAB_MANIFEST = ToolsetManifest(
    id="gitlab",
    version="1.0.0",
    summary="GitLab — projects, issues, merge requests, and notes.",
    description=(
        "GitLab REST API v4, hosted or self-managed. List and search projects, "
        "read and write issues and merge requests, comment, and resolve a "
        "person to the numeric id assignments require. Issue and merge request "
        "paths take the per-project iid, not the global id."
    ),
    base_url="https://gitlab.com/api/v4",
    auth=AuthSpec(
        client="loom.toolsets.gitlab.client:GitLabClient",
        # This client reads environment variables and no CredentialStore, so
        # `credential` is empty and no `provider` is declared: an OAuth flow
        # here would store a token the client never looks up. Adding a store
        # path is a change to the client, not to this manifest.
        kind="bearer",
        fields=(
            AuthField(name="GITLAB_TOKEN", arg="token", label="Personal access token",
                      mode="personal"),
            AuthField(name="GITLAB_OAUTH_TOKEN",
                      arg="oauth_token", label="OAuth token", mode="oauth"),
            AuthField(name="GITLAB_URL", arg="base_url", label="Instance URL (self-managed)",
                      secret=False, required=False,
                      example="https://gitlab.acme.com"),
        ),
    ),
    tools_module="loom.toolsets.gitlab.tools",
    egress_hosts=["gitlab.com", "*.gitlab.com"],
    rate_limits={
        "model": (
            "per-instance and per-endpoint; self-managed instances configure "
            "their own limits, so no fixed rate can be assumed"
        ),
    },
    groups={
        "projects": [
            OperationSpec(
                id="projects.list",
                function="gitlab_list_projects",
                summary="List projects, most recently active first.",
                description="Resolve a project before working in it.",
                resolves="project",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitLabProject),
            ),
            OperationSpec(
                id="projects.get",
                function="gitlab_get_project",
                summary="Fetch one project by id or group/project path.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitLabProject.model_json_schema(),
            ),
        ],
        "issues": [
            OperationSpec(
                id="issues.list",
                function="gitlab_list_issues",
                summary="List issues in a project.",
                description="state is 'opened', not 'open' — GitLab ignores unknown states.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitLabIssue),
            ),
            OperationSpec(
                id="issues.get",
                function="gitlab_get_issue",
                summary="Fetch one issue by its per-project iid.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitLabIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.create",
                function="gitlab_create_issue",
                summary="Open an issue.",
                description="Not retried: GitLab has no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=GitLabIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.update",
                function="gitlab_update_issue",
                summary="Retitle, edit, relabel, close, or reopen an issue.",
                description="Closing takes state_event='close', not a state.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=GitLabIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.close",
                function="gitlab_close_issue",
                summary="Close an issue.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=GitLabIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.list_notes",
                function="gitlab_list_issue_notes",
                summary="List notes on an issue, system records excluded.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitLabNote),
            ),
            OperationSpec(
                id="issues.add_note",
                function="gitlab_add_issue_note",
                summary="Comment on an issue.",
                description="Not retried: a retry double-posts.",
                effect=EffectClass.WRITE,
                output_schema=GitLabNote.model_json_schema(),
            ),
        ],
        "merge_requests": [
            OperationSpec(
                id="merge_requests.list",
                function="gitlab_list_merge_requests",
                summary="List merge requests in a project.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitLabMergeRequest),
            ),
            OperationSpec(
                id="merge_requests.get",
                function="gitlab_get_merge_request",
                summary="Fetch one merge request by its per-project iid.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitLabMergeRequest.model_json_schema(),
            ),
            OperationSpec(
                id="merge_requests.create",
                function="gitlab_create_merge_request",
                summary="Open a merge request.",
                description="Not retried: no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=GitLabMergeRequest.model_json_schema(),
            ),
        ],
        "users": [
            OperationSpec(
                id="users.find",
                function="gitlab_find_users",
                summary="Find people by username, name, or email.",
                description="Resolve before assigning: assignments take numeric ids.",
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(GitLabUser),
            ),
            OperationSpec(
                id="users.whoami",
                function="gitlab_whoami",
                summary="The user this token authenticates as.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitLabUser.model_json_schema(),
            ),
        ],
    },
)
