"""Jira ToolsetManifest for registration with the global catalog.

Output schemas are derived from the Pydantic models in ``models.py``
to keep contracts DRY.
"""

from __future__ import annotations

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
from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
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
    auth=AuthSpec(
        # `atlassian`, not `jira`: the provider serves both Jira and
        # Confluence, and `loom connect jira` refused outright until this
        # said so. `offline_access` is requested by the flow and implied by
        # no single operation, which is why it is here and the rest are on
        # the operations.
        kind="oauth2",
        credential="jira",
        provider="atlassian",
        scopes=("offline_access",),
        fields=(
            AuthField(name="JIRA_URL", label="Site URL", secret=False,
                      example="https://acme.atlassian.net", arg="base_url"),
            AuthField(name="JIRA_EMAIL", label="Atlassian account email",
                      secret=False, arg="email"),
            AuthField(name="JIRA_API_TOKEN", label="API token", arg="api_token"),
        ),
        client="loom.toolsets.jira.client:JiraClient",
        setup_url="https://developer.atlassian.com/console/myapps/",
        docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
    ),
    tools_module="loom.toolsets.jira.tools",
    opaque_ids={
        # The number differs per site and nobody knows it from memory.
        r"customfield_\d+": "field",
    },
    egress_hosts=["*.atlassian.net"],
    rate_limits={
        "model": (
            "points-based: each call consumes points scaled to the complexity "
            "and volume of data, against a per-hour quota — so no fixed "
            "requests-per-second figure applies"
        ),
        "enforcement": (
            "tiered quotas for Forge, Connect and OAuth 2.0 (3LO) apps began "
            "2026-03-02"
        ),
        "source": "developer.atlassian.com/cloud/jira/platform/rate-limiting/",
    },
    groups={
        "issues": [
            OperationSpec(
                id="issues.search",
                function="jira_search_issues",
                summary="Search issues with a JQL query.",
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string"},
                        "max_results": {
                            "type": "integer",
                            "default": 20,
                        },
                        "custom_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "REST ids to fetch as well, e.g. "
                                "customfield_10016. Resolve with fields.resolve."
                            ),
                        },
                    },
                    "required": ["jql"],
                },
                output_schema=_issue_list_schema,
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="issues.resolve_epic",
                function="jira_resolve_epic",
                resolves="epic",
                summary="Find an epic by the name a person calls it.",
                description=(
                    "Call this whenever a request names an epic — 'the "
                    "billing epic'. JQL addresses an epic by issue key, "
                    "never by that name, and nothing joins the two. An epic "
                    "is an issue, so there is no endpoint listing epics and "
                    "this scoped search is the only lookup; matching "
                    "summary ~ inline instead searches every issue on the "
                    "site. Pass project when it is known — epic names repeat "
                    "across projects. Check the match count before filtering."
                ),
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "epic_name": {"type": "string"},
                        "project": {
                            "type": "string",
                            "description": (
                                "Project key to search within. Resolve it "
                                "first with projects.resolve."
                            ),
                        },
                    },
                    "required": ["epic_name"],
                },
                output_schema=EpicLookup.model_json_schema(),
                idempotent=True,
            ),
            OperationSpec(
                id="issues.get",
                function="jira_get_issue",
                summary="Fetch a single issue by key.",
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "custom_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
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
                        "assignee_account_id": {"type": "string"},
                        "custom_fields": {
                            "type": "object",
                            "description": (
                                "Keyed by REST id, in Jira's own value shape. "
                                "Resolve an id with fields.resolve."
                            ),
                        },
                    },
                    "required": ["project_key", "summary"],
                },
                output_schema=CreatedIssue.model_json_schema(),
                scopes=["write:jira-work"],
            ),
            OperationSpec(
                id="issues.update",
                idempotent=True,
                function="jira_update_issue",
                summary="Update fields on an existing issue.",
                description=(
                    "`fields` is the Jira REST payload: named entities are "
                    "objects ({'priority': {'name': 'High'}}), description is "
                    "Atlassian Document Format rather than a string, and a "
                    "custom field is keyed by REST id "
                    "({'customfield_10016': 5}). Resolve a custom field's id "
                    "with fields.resolve; the number differs per instance."
                ),
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
                scopes=["read:jira-work"],
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
                idempotent=True,
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
                scopes=["read:jira-work"],
                pagination=True,
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
                id="projects.resolve",
                function="jira_resolve_project",
                resolves="project",
                summary="Find a project by key or by the name people call it.",
                description=(
                    "A project is the namespace nearly every other query is "
                    "scoped by, and its key is never the words anybody says — "
                    "'Acme Platform' is ACME. JQL's project = accepts either, so "
                    "an exact hit needs no guessing; what this prevents is "
                    "the other case, where a filter on a project that does "
                    "not exist returns zero issues and no error."
                ),
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
                input_schema={
                    "type": "object",
                    "properties": {"project_name": {"type": "string"}},
                    "required": ["project_name"],
                },
                output_schema=ProjectLookup.model_json_schema(),
                idempotent=True,
            ),
            OperationSpec(
                id="projects.list",
                function="jira_list_projects",
                summary="List all accessible projects.",
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
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
                scopes=["read:jira-work"],
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
                scopes=["read:jira-work"],
                input_schema={
                    "type": "object",
                    "properties": {"project_key": {"type": "string"}},
                    "required": ["project_key"],
                },
                output_schema=ProjectMetadata.model_json_schema(),
                idempotent=True,
            ),
        ],
        "fields": [
            OperationSpec(
                id="fields.list",
                function="jira_list_fields",
                summary="List the custom fields this instance defines.",
                description=(
                    "Custom fields are per-instance configuration: 'Story "
                    "Points' is customfield_10016 on one site and "
                    "customfield_10024 on the next. Each row carries `id` "
                    "(what a REST payload uses) and `clause_names` (what JQL "
                    "accepts) — they are not interchangeable."
                ),
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
                pagination=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 200},
                    },
                },
                output_schema={
                    "type": "array",
                    "items": JiraField.model_json_schema(),
                },
                idempotent=True,
            ),
            OperationSpec(
                id="fields.resolve",
                function="jira_resolve_field",
                resolves="field",
                summary="Resolve a custom field's display name to its REST id.",
                description=(
                    "Call before putting a custom field in JQL, a "
                    "custom_fields list, or an update payload. Resolve once "
                    "while authoring and write the id into the code with the "
                    "name in a comment. Check `exact` before writing: 'Story "
                    "Points' and 'Story point estimate' are different fields "
                    "on instances that have both."
                ),
                effect=EffectClass.READ,
                scopes=["read:jira-work"],
                input_schema={
                    "type": "object",
                    "properties": {"field_name": {"type": "string"}},
                    "required": ["field_name"],
                },
                output_schema=FieldLookup.model_json_schema(),
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
                scopes=["read:jira-work"],
                pagination=True,
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
                scopes=["read:jira-work"],
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
                scopes=["read:jira-work"],
                output_schema=JiraUser.model_json_schema(),
                idempotent=True,
            ),
        ],
    },
)
