"""Typed shapes for the Asana API.

Asana identifies everything by ``gid`` — a string, always, even though it looks
like a number. It is kept as ``gid`` rather than renamed to ``id`` because every
Asana URL, error message, and support article calls it that, and a workflow
author reading the docs alongside this code should see the same word.

Every field is optional on the wire: Asana returns only what ``opt_fields``
asked for, so a model built from a bare response has names and gids and little
else. The defaults here are what a caller gets when a field was not requested,
not an assertion that the value is empty in Asana.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AsanaUser(BaseModel):
    """A person."""

    gid: str = ""
    name: str = ""
    email: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> AsanaUser:
        raw = raw or {}
        return cls(
            gid=str(raw.get("gid", "") or ""),
            name=raw.get("name") or "",
            email=raw.get("email") or "",
        )


class AsanaWorkspace(BaseModel):
    """A workspace or organization — the top of the Asana tree."""

    gid: str = ""
    name: str = ""
    is_organization: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> AsanaWorkspace:
        return cls(
            gid=str(raw.get("gid", "")),
            name=raw.get("name") or "",
            is_organization=bool(raw.get("is_organization", False)),
        )


class AsanaProject(BaseModel):
    """A project."""

    gid: str = ""
    name: str = ""
    archived: bool = False
    notes: str = ""
    permalink_url: str = ""
    owner: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> AsanaProject:
        return cls(
            gid=str(raw.get("gid", "")),
            name=raw.get("name") or "",
            archived=bool(raw.get("archived", False)),
            notes=raw.get("notes") or "",
            permalink_url=raw.get("permalink_url") or "",
            owner=(raw.get("owner") or {}).get("name") or "",
        )


class AsanaSection(BaseModel):
    """A section within a project — Asana's column or grouping."""

    gid: str = ""
    name: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> AsanaSection:
        return cls(gid=str(raw.get("gid", "")), name=raw.get("name") or "")


class AsanaTask(BaseModel):
    """One task, flattened to what a workflow reasons about."""

    gid: str
    name: str = ""
    notes: str = ""
    completed: bool = False
    assignee: str = ""
    """The assignee's *name*. The gid is in ``assignee_gid``."""
    assignee_gid: str = ""
    due_on: str = ""
    """A date, ``YYYY-MM-DD``. Asana keeps ``due_on`` and ``due_at`` separate:
    a day without a time, versus an instant."""
    due_at: str = ""
    projects: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    permalink_url: str = ""
    created_at: str = ""
    modified_at: str = ""
    completed_at: str = ""
    parent: str = ""
    """Set when this task is a subtask."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> AsanaTask:
        assignee = raw.get("assignee") or {}
        return cls(
            gid=str(raw.get("gid", "")),
            name=raw.get("name") or "",
            notes=raw.get("notes") or "",
            completed=bool(raw.get("completed", False)),
            assignee=assignee.get("name") or "",
            assignee_gid=str(assignee.get("gid", "") or ""),
            due_on=raw.get("due_on") or "",
            due_at=raw.get("due_at") or "",
            projects=[p.get("name") or "" for p in (raw.get("projects") or [])],
            tags=[t.get("name") or "" for t in (raw.get("tags") or [])],
            permalink_url=raw.get("permalink_url") or "",
            created_at=raw.get("created_at") or "",
            modified_at=raw.get("modified_at") or "",
            completed_at=raw.get("completed_at") or "",
            parent=str((raw.get("parent") or {}).get("gid", "") or ""),
        )


class AsanaStory(BaseModel):
    """A story — Asana's term for an entry in a task's activity feed.

    Comments are stories with ``type="comment"``; the rest are system records
    of the task changing. ``asana_list_comments`` filters to the former,
    because "show me the comments" almost never means "show me every field
    anyone has ever edited".
    """

    gid: str = ""
    text: str = ""
    type: str = ""
    author: str = ""
    created_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> AsanaStory:
        return cls(
            gid=str(raw.get("gid", "")),
            text=raw.get("text") or "",
            type=raw.get("type") or raw.get("resource_subtype") or "",
            author=(raw.get("created_by") or {}).get("name") or "",
            created_at=raw.get("created_at") or "",
        )
