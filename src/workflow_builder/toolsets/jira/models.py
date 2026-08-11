"""Typed Pydantic response models for the Jira toolset.

These are the single source of truth for all Jira response shapes.
The client, tools, manifest, and auto-generated docs all reference
these models — keeping contracts DRY.

Each model's ``model_dump()`` produces the exact dict shape that the
pre-typed client used to return, so all existing code continues to
work without changes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JiraIssue(BaseModel):
    """A flattened Jira issue, as returned by search and get operations."""

    model_config = ConfigDict(frozen=True)

    key: str
    id: str = ""
    summary: str = ""
    status: str = ""
    assignee: str = "Unassigned"
    priority: str = ""
    issue_type: str = ""
    project: str = ""
    labels: list[str] = Field(default_factory=list)
    created: str = ""
    updated: str = ""
    url: str = ""


class CreatedIssue(BaseModel):
    """Minimal response from creating an issue."""

    model_config = ConfigDict(frozen=True)

    key: str
    id: str
    url: str = ""


class Comment(BaseModel):
    """Response from adding a comment."""

    model_config = ConfigDict(frozen=True)

    id: str
    author: str = ""
    created: str = ""


class Transition(BaseModel):
    """An available status transition on an issue."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str


class JiraProject(BaseModel):
    """Compact project info from list_projects()."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    id: str


class JiraProjectDetail(BaseModel):
    """Extended project info from get_project()."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    id: str
    description: str = ""
    lead: str = ""


class JiraUser(BaseModel):
    """Authenticated Jira user profile."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    email: str = ""


__all__ = [
    "Comment",
    "CreatedIssue",
    "JiraIssue",
    "JiraProject",
    "JiraProjectDetail",
    "JiraUser",
    "Transition",
]
