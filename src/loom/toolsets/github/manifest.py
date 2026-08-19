"""GitHub ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.github.models import (
    GitHubComment,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRepo,
    GitHubUser,
)
from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


GITHUB_MANIFEST = ToolsetManifest(
    id="github",
    version="1.0.0",
    summary="GitHub — repositories, issues, pull requests, comments, and search.",
    description=(
        "GitHub REST API. List and search repositories, issues, and pull "
        "requests; open, update, and comment on them; and resolve a person to "
        "the login every assignment takes. Issue listings exclude pull "
        "requests by default, because GitHub returns both from that endpoint."
    ),
    base_url="https://api.github.com",
    auth={"type": "bearer", "fields": ["GITHUB_TOKEN", "GITHUB_API_URL"]},
    tools_module="loom.toolsets.github.tools",
    egress_hosts=["api.github.com", "*.githubusercontent.com"],
    rate_limits={
        "search": "30 requests per minute",
        "primary": (
            "quota reported by x-ratelimit-remaining; a 403 with zero "
            "remaining is retryable, any other 403 never is"
        ),
    },
    groups={
        "repos": [
            OperationSpec(
                id="repos.list",
                function="github_list_repos",
                summary="List repositories for an owner, or your own.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitHubRepo),
            ),
            OperationSpec(
                id="repos.get",
                function="github_get_repo",
                summary="Fetch one repository by owner/repo.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitHubRepo.model_json_schema(),
            ),
        ],
        "issues": [
            OperationSpec(
                id="issues.list",
                function="github_list_issues",
                summary="List issues in a repository.",
                description=(
                    "Pull requests are excluded by default: GitHub returns "
                    "them from this endpoint, so an unfiltered count of 'open "
                    "issues' includes PRs."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitHubIssue),
            ),
            OperationSpec(
                id="issues.get",
                function="github_get_issue",
                summary="Fetch one issue by number.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitHubIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.create",
                function="github_create_issue",
                summary="Open an issue.",
                description="Not retried: GitHub has no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=GitHubIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.update",
                function="github_update_issue",
                summary="Retitle, edit, close, reopen, or relabel an issue.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=GitHubIssue.model_json_schema(),
            ),
            OperationSpec(
                id="issues.list_comments",
                function="github_list_comments",
                summary="List comments on an issue or pull request.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitHubComment),
            ),
            OperationSpec(
                id="issues.add_comment",
                function="github_add_comment",
                summary="Comment on an issue or pull request.",
                description="Not retried: a retry double-posts.",
                effect=EffectClass.WRITE,
                output_schema=GitHubComment.model_json_schema(),
            ),
        ],
        "pulls": [
            OperationSpec(
                id="pulls.list",
                function="github_list_pull_requests",
                summary="List pull requests in a repository.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitHubPullRequest),
            ),
            OperationSpec(
                id="pulls.get",
                function="github_get_pull_request",
                summary="Fetch one pull request by number.",
                description=(
                    "Use this rather than an id from an issues listing, which "
                    "is an issue id and addresses something else."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitHubPullRequest.model_json_schema(),
            ),
            OperationSpec(
                id="pulls.create",
                function="github_create_pull_request",
                summary="Open a pull request.",
                description="Not retried: no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=GitHubPullRequest.model_json_schema(),
            ),
        ],
        "search": [
            OperationSpec(
                id="search.issues",
                function="github_search_issues",
                summary="Search issues and pull requests across GitHub.",
                description=(
                    "Capped at 1,000 results and 30 requests/minute, and "
                    "flagged incomplete on a server-side timeout. All three "
                    "surface through .complete."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitHubIssue),
            ),
            OperationSpec(
                id="search.repos",
                function="github_search_repos",
                summary="Search repositories across GitHub.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(GitHubRepo),
            ),
        ],
        "users": [
            OperationSpec(
                id="users.find",
                function="github_find_users",
                summary="Find people by name, login, or email.",
                description="Resolve before assigning: assignments take a login.",
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(GitHubUser),
            ),
            OperationSpec(
                id="users.whoami",
                function="github_whoami",
                summary="The user this token authenticates as.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=GitHubUser.model_json_schema(),
            ),
        ],
    },
)
