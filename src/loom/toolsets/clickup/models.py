"""Typed shapes for the ClickUp API v2.

Flattened deliberately. ClickUp nests almost everything — a status is
``{"status": "in progress", "type": "custom", "orderindex": 1, ...}``, an
assignee is an object inside a list — and handing that to a model to reason
about costs tokens on structure nobody asked about. What a workflow wants is
``task.status`` and ``task.assignees``, so the flattening happens here, once,
where the wire shape is known.

Every model tolerates missing fields: ClickUp omits keys rather than sending
nulls, and varies which ones by endpoint (a task from a list view carries less
than a task fetched by id).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _ms_to_iso(value: Any) -> str:
    """ClickUp timestamps are Unix milliseconds *as strings*.

    Returned as ISO-8601 because that is what every other toolset in this
    repository returns, and a model comparing a due date against ``ctx.now()``
    should not have to know that one vendor counts milliseconds.
    """
    if value in (None, "", 0, "0"):
        return ""
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


class ClickUpUser(BaseModel):
    """A person, as ClickUp reports them on a task or in a workspace."""

    id: str = ""
    username: str = ""
    email: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> ClickUpUser:
        raw = raw or {}
        return cls(
            id=str(raw.get("id", "") or ""),
            username=raw.get("username") or "",
            email=raw.get("email") or "",
        )


class ClickUpTask(BaseModel):
    """One task, flattened to what a workflow reasons about."""

    id: str
    name: str = ""
    description: str = ""
    status: str = ""
    """The status *name* — "in progress" — not the status object."""
    url: str = ""
    list_id: str = ""
    list_name: str = ""
    assignees: list[str] = Field(default_factory=list)
    """Usernames, not ids. Ids are in ``assignee_ids`` for a follow-up call."""
    assignee_ids: list[str] = Field(default_factory=list)
    priority: str = ""
    tags: list[str] = Field(default_factory=list)
    due_date: str = ""
    start_date: str = ""
    date_created: str = ""
    date_updated: str = ""
    creator: str = ""
    parent: str = ""
    """Set when this task is a subtask."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ClickUpTask:
        assignees = raw.get("assignees") or []
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            description=raw.get("description") or raw.get("text_content") or "",
            status=(raw.get("status") or {}).get("status") or "",
            url=raw.get("url") or "",
            list_id=str((raw.get("list") or {}).get("id", "") or ""),
            list_name=(raw.get("list") or {}).get("name") or "",
            assignees=[a.get("username") or "" for a in assignees],
            assignee_ids=[str(a.get("id", "")) for a in assignees],
            # A task with no priority has `priority: null`, not a missing key.
            priority=((raw.get("priority") or {}).get("priority") or ""),
            tags=[t.get("name") or "" for t in (raw.get("tags") or [])],
            due_date=_ms_to_iso(raw.get("due_date")),
            start_date=_ms_to_iso(raw.get("start_date")),
            date_created=_ms_to_iso(raw.get("date_created")),
            date_updated=_ms_to_iso(raw.get("date_updated")),
            creator=(raw.get("creator") or {}).get("username") or "",
            parent=str(raw.get("parent") or "" or ""),
        )


class ClickUpComment(BaseModel):
    """A comment on a task."""

    id: str
    text: str = ""
    author: str = ""
    date: str = ""
    resolved: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ClickUpComment:
        return cls(
            id=str(raw.get("id", "")),
            # `comment_text` is the plain rendering; `comment` is a list of
            # rich-text fragments that no caller has asked for yet.
            text=raw.get("comment_text") or "",
            author=(raw.get("user") or {}).get("username") or "",
            date=_ms_to_iso(raw.get("date")),
            resolved=bool(raw.get("resolved", False)),
        )


class ClickUpWorkspace(BaseModel):
    """A ClickUp workspace. The API calls these "teams" in its URLs."""

    id: str
    name: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ClickUpWorkspace:
        return cls(id=str(raw.get("id", "")), name=raw.get("name") or "")


class ClickUpContainer(BaseModel):
    """A space, folder, or list — the three levels above a task.

    One model for all three because they carry the same fields and differ only
    in what contains them, and a workflow navigating down does the same thing at
    each level. ``kind`` says which it is.
    """

    id: str
    name: str = ""
    kind: str = ""
    """``space``, ``folder``, or ``list``."""
    task_count: int = 0
    archived: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any], kind: str) -> ClickUpContainer:
        # ClickUp sends this as a string on some endpoints and omits it on
        # others, so it is normalised to text before being read as a number.
        count = str(raw.get("task_count") or "")
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            kind=kind,
            task_count=int(count) if count.isdigit() else 0,
            archived=bool(raw.get("archived", False)),
        )
