"""Typed shapes for the GitHub REST API.

Two GitHub facts drive these models, and both produce wrong answers rather than
errors when ignored.

**Every pull request is an issue.** ``/repos/{owner}/{repo}/issues`` returns
both, told apart by a ``pull_request`` key, so :class:`GitHubIssue` carries
``is_pull_request`` rather than leaving a caller to notice a missing field.

**``number`` is not ``id``.** The number is what a URL and a person use
(``#412``); the id is a global integer that almost nothing takes. Both are
carried, named as GitHub names them, because silently exposing one as "the id"
is how a caller ends up addressing the wrong resource.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GitHubUser(BaseModel):
    """A person or an app."""

    login: str = ""
    id: str = ""
    name: str = ""
    email: str = ""
    type: str = ""
    url: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> GitHubUser:
        raw = raw or {}
        return cls(
            login=raw.get("login") or "",
            id=str(raw.get("id", "") or ""),
            name=raw.get("name") or "",
            email=raw.get("email") or "",
            type=raw.get("type") or "",
            url=raw.get("html_url") or "",
        )


class GitHubRepo(BaseModel):
    """A repository."""

    id: str = ""
    name: str = ""
    full_name: str = ""
    """``owner/repo`` — the handle every other path takes."""
    description: str = ""
    private: bool = False
    default_branch: str = ""
    language: str = ""
    stars: int = 0
    open_issues: int = 0
    url: str = ""
    updated_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitHubRepo:
        return cls(
            id=str(raw.get("id", "") or ""),
            name=raw.get("name") or "",
            full_name=raw.get("full_name") or "",
            description=raw.get("description") or "",
            private=bool(raw.get("private", False)),
            default_branch=raw.get("default_branch") or "",
            language=raw.get("language") or "",
            stars=int(raw.get("stargazers_count") or 0),
            # Counts pull requests too, exactly as the issues listing does.
            open_issues=int(raw.get("open_issues_count") or 0),
            url=raw.get("html_url") or "",
            updated_at=raw.get("updated_at") or "",
        )


class GitHubIssue(BaseModel):
    """An issue — or a pull request, which GitHub also returns here."""

    number: int = 0
    """What a URL and a person use. Not ``id``."""
    id: str = ""
    title: str = ""
    body: str = ""
    state: str = ""
    author: str = ""
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    comments: int = 0
    created_at: str = ""
    updated_at: str = ""
    closed_at: str = ""
    url: str = ""
    is_pull_request: bool = False
    """True when this row is really a pull request.

    GitHub's own words: every pull request is an issue, but not every issue is
    a pull request. A caller counting "open issues" over an unfiltered listing
    is counting PRs too."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitHubIssue:
        return cls(
            number=int(raw.get("number") or 0),
            id=str(raw.get("id", "") or ""),
            title=raw.get("title") or "",
            body=raw.get("body") or "",
            state=raw.get("state") or "",
            author=(raw.get("user") or {}).get("login") or "",
            assignees=[a.get("login") or "" for a in (raw.get("assignees") or [])],
            # GitHub sends label objects; GitLab sends plain strings. Both
            # shapes appear in the wild here because a webhook payload is not
            # always the REST shape.
            labels=[
                str(label.get("name") or "") if isinstance(label, dict) else str(label)
                for label in (raw.get("labels") or [])
            ],
            comments=int(raw.get("comments") or 0),
            created_at=raw.get("created_at") or "",
            updated_at=raw.get("updated_at") or "",
            closed_at=raw.get("closed_at") or "",
            url=raw.get("html_url") or "",
            is_pull_request="pull_request" in raw,
        )


class GitHubPullRequest(BaseModel):
    """A pull request, from the pull request endpoints.

    Distinct from :class:`GitHubIssue` because the branches and merge state
    only exist here — and because an id taken from an *issues* listing is an
    issue id and does not address a pull request at all.
    """

    number: int = 0
    id: str = ""
    title: str = ""
    body: str = ""
    state: str = ""
    author: str = ""
    head: str = ""
    base: str = ""
    draft: bool = False
    merged: bool = False
    mergeable_state: str = ""
    url: str = ""
    created_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitHubPullRequest:
        return cls(
            number=int(raw.get("number") or 0),
            id=str(raw.get("id", "") or ""),
            title=raw.get("title") or "",
            body=raw.get("body") or "",
            state=raw.get("state") or "",
            author=(raw.get("user") or {}).get("login") or "",
            head=(raw.get("head") or {}).get("ref") or "",
            base=(raw.get("base") or {}).get("ref") or "",
            draft=bool(raw.get("draft", False)),
            merged=bool(raw.get("merged", False)),
            mergeable_state=raw.get("mergeable_state") or "",
            url=raw.get("html_url") or "",
            created_at=raw.get("created_at") or "",
        )


class GitHubComment(BaseModel):
    """A comment on an issue or pull request."""

    id: str = ""
    body: str = ""
    author: str = ""
    created_at: str = ""
    url: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> GitHubComment:
        return cls(
            id=str(raw.get("id", "") or ""),
            body=raw.get("body") or "",
            author=(raw.get("user") or {}).get("login") or "",
            created_at=raw.get("created_at") or "",
            url=raw.get("html_url") or "",
        )
