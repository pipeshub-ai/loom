"""Typed Pydantic response models for the Jira toolset.

These are the single source of truth for all Jira response shapes.
The client, tools, manifest, and auto-generated docs all reference
these models — keeping contracts DRY.

Each model's ``model_dump()`` produces the exact dict shape that the
pre-typed client used to return, so all existing code continues to
work without changes.
"""

from __future__ import annotations

from typing import Any

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
    due_date: str = ""
    """Jira's ``duedate``, ``YYYY-MM-DD``, empty when the issue has none.

    First-class rather than something to ask for: it is a system field, so
    ``jira_resolve_field("Due date")`` reports it as one and says no
    ``custom_fields`` entry is needed to read it. That sentence has to be
    true of every system field the lookup says it about.
    """
    url: str = ""
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    """Whatever ``customfield_*`` the request asked for, in Jira's own shape.

    Empty unless a read named the fields it wanted: Jira returns only what
    ``fields=`` asks for, so this is populated by ``custom_fields=[...]`` on
    the read and not otherwise. Keys are the REST ids that were asked for —
    ``customfield_10016``, and equally a system id like ``resolutiondate``
    that no attribute above carries. Never the display name; resolve one
    with ``jira_resolve_field``.

    A key present with a ``None`` value means the read asked and the issue
    has nothing there — a different fact from the key being absent, which
    means nobody asked.

    Values are raw: a number field is a number, a select is
    ``{"value": "High", "id": "10001"}``, a user is an account object. The
    toolset does not flatten them, because the shape depends on how the
    instance configured the field and guessing wrong would lose data.
    """


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


class Lookup(BaseModel):
    """What resolving one of a human's words against one namespace produced.

    Every resolver here answers the same three questions and differs only in
    what it matched *against*, so the answer is one shape with a typed
    :attr:`matches`. Two of those questions are the reason a resolver exists at
    all rather than a caller filtering on the word directly:

    :attr:`exact` separates a literal hit from a near-miss guess. Resolving a
    misspelling to the nearest candidate is reasonable for a read and reckless
    for a write, and that is the caller's decision rather than the resolver's.

    The count of :attr:`matches` separates the two failures that a filter on a
    raw word collapses into one empty result: nothing bears this name, and
    several things do. They call for opposite responses — say so, versus pick
    one — and neither is "return no rows".
    """

    model_config = ConfigDict(frozen=True)

    query: str
    exact: bool = False
    """True when a name matched literally; False when this is a near-miss guess."""
    note: str = ""
    """Human-readable account of what happened, for an agent to relay or act on."""


class UserLookup(Lookup):
    """The result of resolving a person's name, typos included.

    Carries whether the match was literal so a caller can decide how much to
    trust it. Silently resolving a misspelling to the nearest human is fine for
    a read and dangerous for a write.
    """

    matches: list[JiraUser] = Field(default_factory=list)


class JiraField(BaseModel):
    """One field this instance defines, system or custom.

    **Two identifiers, and they are not interchangeable.** :attr:`id` is what a
    REST payload uses — ``customfield_10016`` in an update body or in the
    ``custom_fields=`` list on a read. :attr:`clause_names` is what JQL
    accepts — ``"Story Points"``, ``cf[10016]``. Putting the REST id in JQL
    matches nothing *and does not fail*, which is why both are carried rather
    than one being derived from the other.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The REST id. ``customfield_10016`` for a custom field, ``summary`` for a
    system one."""
    name: str = ""
    """The display name a person sees, and the word a spec will use."""
    custom: bool = False
    field_type: str = ""
    """Jira's own schema type: ``number``, ``string``, ``option``, ``array``."""
    custom_id: int | None = None
    """The numeric half of a custom field's id, as JQL's ``cf[10016]`` form."""
    clause_names: list[str] = Field(default_factory=list)
    """Every name JQL will accept for this field."""


class FieldLookup(Lookup):
    """The result of resolving a field's display name to its REST id.

    A near match is worth returning, and worth labelling as a near match.
    "Story Points" and "Story point estimate" are different fields on different
    instances, and silently picking one writes to the wrong column.
    """

    matches: list[JiraField] = Field(default_factory=list)


class ProjectLookup(Lookup):
    """The result of resolving a project's name to its key.

    A project is the namespace almost every other Jira query is scoped by, and
    its key is never the word anybody says — "Acme Platform" is ``ACME``. Nothing
    joins the two, so a JQL clause built from the spoken name matches no
    project and Jira reports that as zero issues rather than as a bad filter.
    """

    matches: list[JiraProject] = Field(default_factory=list)


class EpicLookup(Lookup):
    """The result of resolving an epic's name to its issue key.

    Separate from a plain search because an epic is a *container* people name
    in conversation — "the billing epic" — while Jira addresses it by an issue key
    nobody knows from memory. It is also the awkward case among containers: an
    epic **is** an issue, so unlike a project or a board there is no endpoint
    that lists epics, and the only lookup is a scoped JQL search. That makes it
    the one container a caller is most likely to give up on and text-match
    instead, which is exactly the guess resolution exists to prevent.
    """

    matches: list[JiraIssue] = Field(default_factory=list)


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
    "EpicLookup",
    "FieldLookup",
    "JiraField",
    "JiraIssue",
    "JiraProject",
    "JiraProjectDetail",
    "JiraUser",
    "Lookup",
    "ProjectLookup",
    "ProjectMetadata",
    "Transition",
    "UserLookup",
]
