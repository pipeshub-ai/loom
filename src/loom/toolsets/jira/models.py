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
    """A comment, as written or as read back."""

    model_config = ConfigDict(frozen=True)

    id: str
    author: str = ""
    created: str = ""
    body: str = ""
    """Plain text, flattened out of Atlassian Document Format."""


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
    """A Jira user, from the authenticated profile or a search."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    email: str = ""
    active: bool = True
    """Deactivated accounts still match a name search and own old issues."""


class UserLookup(BaseModel):
    """The result of resolving a person's name, typos included.

    Carries whether the match was literal so a caller can decide how much to
    trust it. Silently resolving a misspelling to the nearest human is fine for
    a read and dangerous for a write.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    matches: list[JiraUser] = Field(default_factory=list)
    exact: bool = False
    """True when Jira's own search matched; False when this is a near-miss guess."""
    note: str = ""
    """Human-readable account of what happened, for an agent to relay or act on."""


class ProjectMetadata(BaseModel):
    """The values a JQL query about a project may legally use.

    Status and priority names are per-project configuration, not constants.
    A query filtering on "In Progress" against a board that calls it "Testing"
    returns zero rows and no error, which reads as "nothing to do" when it
    means "wrong word".
    """

    model_config = ConfigDict(frozen=True)

    project_key: str
    statuses: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    issue_types: list[str] = Field(default_factory=list)


__all__ = [
    "Comment",
    "CreatedIssue",
    "JiraIssue",
    "JiraProject",
    "JiraProjectDetail",
    "JiraUser",
    "ProjectMetadata",
    "Transition",
    "UserLookup",
]
