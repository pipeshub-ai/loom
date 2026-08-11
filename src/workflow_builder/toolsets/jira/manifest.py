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
    JiraUser,
    Transition,
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
    egress_hosts=["*.atlassian.net"],
    groups={
        "issues": [
            OperationSpec(
                id="issues.search",
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
        ],
        "projects": [
            OperationSpec(
                id="projects.list",
                summary="List all accessible projects.",
                effect=EffectClass.READ,
                output_schema={
                    "type": "array",
                    "items": JiraProject.model_json_schema(),
                },
                idempotent=True,
            ),
        ],
        "users": [
            OperationSpec(
                id="users.myself",
                summary="Get the authenticated user's profile.",
                effect=EffectClass.READ,
                output_schema=JiraUser.model_json_schema(),
                idempotent=True,
            ),
        ],
    },
)
