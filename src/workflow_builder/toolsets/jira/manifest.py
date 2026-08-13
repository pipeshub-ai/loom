"""Jira ToolsetManifest for registration with the global catalog.

Output schemas are derived from the Pydantic models in ``models.py``
to keep contracts DRY.
"""

from __future__ import annotations

from workflow_builder.toolsets.jira.models import (
    Comment,
    CreatedIssue,
    JiraIssue,
    JiraProject,
    JiraProjectDetail,
    JiraUser,
    ProjectMetadata,
    Transition,
    UserLookup,
)
from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

_issue_schema = JiraIssue.model_json_schema()
_issue_list_schema = {
    "type": "array",
    "items": _issue_schema,
}

JIRA_MANIFEST = ToolsetManifest(
    id="jira",
    version="1.0.0",
    summary="Jira issue tracker — search, create, update, transition, comment.",
    description=(
        "Full Jira REST API v3 integration. Supports issue search (JQL), "
        "create/update/transition issues, add comments, list projects, "
        "and get the authenticated user."
    ),
    base_url="https://<org>.atlassian.net",
    auth={
        "type": "basic",
        "fields": ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
    },
    tools_module="workflow_builder.toolsets.jira.tools",
    egress_hosts=["*.atlassian.net"],
    groups={
        "issues": [
            OperationSpec(
                id="issues.search",
                function="jira_search_issues",
                summary="Search issues with a JQL query.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string"},
                        "max_results": {
                            "type": "integer",
                            "default": 20,
                        },
                    },
                    "required": ["jql"],
                },
                output_schema=_issue_list_schema,
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="issues.get",
                function="jira_get_issue",
                summary="Fetch a single issue by key.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                    },
                    "required": ["issue_key"],
                },
                output_schema=_issue_schema,
                idempotent=True,
            ),
            OperationSpec(
                id="issues.create",
                function="jira_create_issue",
                summary="Create a new issue.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_key": {"type": "string"},
                        "summary": {"type": "string"},
                        "description": {"type": "string"},
                        "issue_type": {
                            "type": "string",
                            "default": "Story",
                        },
                        "priority": {
                            "type": "string",
                            "default": "Medium",
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["project_key", "summary"],
                },
                output_schema=CreatedIssue.model_json_schema(),
                scopes=["write:jira-work"],
            ),
            OperationSpec(
                id="issues.update",
                function="jira_update_issue",
                summary="Update fields on an existing issue.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "fields": {"type": "object"},
                    },
                    "required": ["issue_key", "fields"],
                },
                output_schema=_issue_schema,
                scopes=["write:jira-work"],
            ),
            OperationSpec(
                id="issues.add_comment",
                function="jira_add_comment",
                summary="Add a comment to an issue.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "comment": {"type": "string"},
                    },
                    "required": ["issue_key", "comment"],
                },
                output_schema=Comment.model_json_schema(),
                scopes=["write:jira-work"],
            ),
            OperationSpec(
                id="issues.get_transitions",
                function="jira_get_transitions",
                summary="List available status transitions.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                    },
                    "required": ["issue_key"],
                },
                output_schema={
                    "type": "array",
                    "items": Transition.model_json_schema(),
                },
                idempotent=True,
            ),
            OperationSpec(
                id="issues.transition",
                function="jira_transition_issue",
                summary="Move an issue to a new status.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "transition_name": {"type": "string"},
                    },
                    "required": ["issue_key", "transition_name"],
                },
                output_schema=_issue_schema,
                scopes=["write:jira-work"],
            ),
            OperationSpec(
                id="issues.assign",
                function="jira_assign_issue",
                summary="Assign an issue, or unassign it with null.",
                description="Takes an accountId, not a display name.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "account_id": {"type": ["string", "null"]},
                    },
                    "required": ["issue_key", "account_id"],
                },
                output_schema=_issue_schema,
                scopes=["write:jira-work"],
                idempotent=True,
            ),
            OperationSpec(
                id="issues.get_comments",
                function="jira_get_comments",
                summary="Read an issue's comments.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "max_results": {"type": "integer", "default": 20},
                    },
                    "required": ["issue_key"],
                },
                output_schema={"type": "array", "items": Comment.model_json_schema()},
                idempotent=True,
            ),
            OperationSpec(
                id="issues.delete",
                function="jira_delete_issue",
                summary="Permanently delete an issue. No undo.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "delete_subtasks": {"type": "boolean", "default": False},
                    },
                    "required": ["issue_key"],
                },
                output_schema={"type": "string"},
                scopes=["delete:jira-work"],
            ),
        ],
        "projects": [
            OperationSpec(
                id="projects.list",
                function="jira_list_projects",
                summary="List all accessible projects.",
                effect=EffectClass.READ,
                output_schema={
                    "type": "array",
                    "items": JiraProject.model_json_schema(),
                },
                idempotent=True,
            ),
            OperationSpec(
                id="projects.get",
                function="jira_get_project",
                summary="Get one project's details.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"project_key": {"type": "string"}},
                    "required": ["project_key"],
                },
                output_schema=JiraProjectDetail.model_json_schema(),
                idempotent=True,
            ),
            OperationSpec(
                id="projects.metadata",
                function="jira_get_project_metadata",
                summary="The status, priority, and issue-type names this project uses.",
                description=(
                    "Call before filtering a search. These names are per-project "
                    "configuration, and a JQL filter naming one the board does "
                    "not have returns zero rows with no error."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"project_key": {"type": "string"}},
                    "required": ["project_key"],
                },
                output_schema=ProjectMetadata.model_json_schema(),
                idempotent=True,
            ),
        ],
        "users": [
            OperationSpec(
                id="users.search",
                function="jira_search_users",
                summary="Find users by display name or email.",
                description=(
                    "Resolve a person's name to an accountId before using them "
                    "in JQL. A display name in JQL works until two people share "
                    "one or somebody is renamed, and then it silently matches "
                    "nothing rather than failing."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
                output_schema={
                    "type": "array",
                    "items": JiraUser.model_json_schema(),
                },
                idempotent=True,
            ),
            OperationSpec(
                id="users.resolve",
                function="jira_resolve_user",
                resolves="user",
                summary="Find a person by name, tolerating a misspelling.",
                description=(
                    "Prefer over users.search when the name came from a human. "
                    "Jira's search is a substring match, so one wrong letter "
                    "returns nothing and reads as 'no such person'. Check "
                    "`exact` before acting on a write."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                output_schema=UserLookup.model_json_schema(),
                idempotent=True,
            ),
            OperationSpec(
                id="users.myself",
                function="jira_get_myself",
                summary="Get the authenticated user's profile.",
                effect=EffectClass.READ,
                output_schema=JiraUser.model_json_schema(),
                idempotent=True,
            ),
        ],
    },
)
