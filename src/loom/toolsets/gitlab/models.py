"""Typed shapes for the GitLab REST API v4.

GitLab's own version of the id trap, and the reason both numbers are carried
here with GitLab's names rather than one being exposed as "the id":

**``iid`` is not ``id``.** The number in a URL — ``/issues/42`` — is the
``iid``, scoped to one project. The ``id`` is a global integer that most
endpoints do not accept. Passing an ``id`` where an ``iid`` belongs addresses a
different issue in the same project, or none, and reports no error either way.

A project is likewise addressable two ways: its numeric ``id``, or its
URL-encoded ``path_with_namespace`` (``group%2Fproject``). Both are carried.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _people(raw: Any) -> list[str]:
    """Usernames from GitLab's assignee lists, which are objects."""
    return [person.get("username") or "" for person in (raw or [])]


class GitLabUser(BaseModel):
    """A person."""

    id: str = ""
    username: str = ""
    name: str = ""
    email: str = ""
    state: str = ""
    web_url: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> GitLabUser:
        raw = raw or {}
        return cls(
            id=str(raw.get("id", "") or ""),
            username=raw.get("username") or "",
            name=raw.get("name") or "",
            email=raw.get("email") or raw.get("public_email") or "",
            state=raw.get("state") or "",
            web_url=raw.get("web_url") or "",
        )


class GitLabProject(BaseModel):
    """A project — GitLab's repository."""

    id: str = ""
    """Numeric, and what every path takes."""
    path_with_namespace: str = ""
    """``group/project`` — the human handle, URL-encode it to use as a path."""
    name: str = ""
    description: str = ""
    visibility: str = ""
    default_branch: str = ""
    star_count: int = 0
    open_issues_count: int = 0
    web_url: str = ""
    last_activity_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitLabProject:
        return cls(
            id=str(raw.get("id", "") or ""),
            path_with_namespace=raw.get("path_with_namespace") or "",
            name=raw.get("name") or "",
            description=raw.get("description") or "",
            visibility=raw.get("visibility") or "",
            default_branch=raw.get("default_branch") or "",
            star_count=int(raw.get("star_count") or 0),
            open_issues_count=int(raw.get("open_issues_count") or 0),
            web_url=raw.get("web_url") or "",
            last_activity_at=raw.get("last_activity_at") or "",
        )


class GitLabIssue(BaseModel):
    """An issue."""

    iid: int = 0
    """The per-project number, and what every issue path takes."""
    id: str = ""
    """The global id. Carried because GitLab reports it, used by almost nothing."""
    project_id: str = ""
    title: str = ""
    description: str = ""
    state: str = ""
    author: str = ""
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    closed_at: str = ""
    web_url: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitLabIssue:
        return cls(
            iid=int(raw.get("iid") or 0),
            id=str(raw.get("id", "") or ""),
            project_id=str(raw.get("project_id", "") or ""),
            title=raw.get("title") or "",
            description=raw.get("description") or "",
            state=raw.get("state") or "",
            author=(raw.get("author") or {}).get("username") or "",
            assignees=_people(raw.get("assignees")),
            # GitLab sends labels as plain strings, unlike GitHub's objects.
            labels=[str(label) for label in (raw.get("labels") or [])],
            created_at=raw.get("created_at") or "",
            updated_at=raw.get("updated_at") or "",
            closed_at=raw.get("closed_at") or "",
            web_url=raw.get("web_url") or "",
        )


class GitLabMergeRequest(BaseModel):
    """A merge request — GitLab's pull request."""

    iid: int = 0
    id: str = ""
    project_id: str = ""
    title: str = ""
    description: str = ""
    state: str = ""
    author: str = ""
    source_branch: str = ""
    target_branch: str = ""
    draft: bool = False
    merge_status: str = ""
    web_url: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitLabMergeRequest:
        return cls(
            iid=int(raw.get("iid") or 0),
            id=str(raw.get("id", "") or ""),
            project_id=str(raw.get("project_id", "") or ""),
            title=raw.get("title") or "",
            description=raw.get("description") or "",
            state=raw.get("state") or "",
            author=(raw.get("author") or {}).get("username") or "",
            source_branch=raw.get("source_branch") or "",
            target_branch=raw.get("target_branch") or "",
            draft=bool(raw.get("draft", raw.get("work_in_progress", False))),
            merge_status=raw.get("merge_status") or "",
            web_url=raw.get("web_url") or "",
        )


class GitLabNote(BaseModel):
    """A note — GitLab's comment.

    ``system`` separates what a person wrote from what GitLab recorded: a note
    saying "changed the milestone" is a system note, and "show me the comments"
    does not mean it.
    """

    id: str = ""
    body: str = ""
    author: str = ""
    system: bool = False
    created_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitLabNote:
        return cls(
            id=str(raw.get("id", "") or ""),
            body=raw.get("body") or "",
            author=(raw.get("author") or {}).get("username") or "",
            system=bool(raw.get("system", False)),
            created_at=raw.get("created_at") or "",
        )
