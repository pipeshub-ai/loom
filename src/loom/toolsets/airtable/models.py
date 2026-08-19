"""Typed rows for the Airtable toolset."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["AirtableField", "AirtableRecord", "AirtableTable"]


class AirtableRecord(BaseModel):
    """One row.

    ``fields`` is keyed by **field name** unless the read asked for ids. That
    is Airtable's default and the single most breakable thing about it: renaming
    a column in the UI changes the key, and a workflow reading the old name gets
    ``None`` rather than an error.
    """

    id: str
    """``rec…``. Stable across renames, unlike every key in ``fields``."""

    fields: dict[str, Any] = Field(default_factory=dict)
    created_time: datetime | None = None

    def get(self, name: str, default: Any = None) -> Any:
        """One field's value.

        Airtable **omits** an empty field rather than sending null, so a
        missing key means "empty", not "no such column" — the two are
        indistinguishable in a response, which is why
        ``airtable_list_fields`` exists.
        """
        return self.fields.get(name, default)

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> AirtableRecord:
        created = body.get("createdTime")
        stamp: datetime | None = None
        if isinstance(created, str):
            try:
                stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                stamp = None
        return cls(
            id=str(body.get("id", "")),
            fields=dict(body.get("fields") or {}),
            created_time=stamp,
        )


class AirtableField(BaseModel):
    """One column's identity.

    Both halves, because a write may use either and only one survives a rename.
    """

    id: str
    """``fld…``. Stable."""

    name: str
    """What the UI shows, and what a response is keyed by. Not stable."""

    type: str = ""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> AirtableField:
        return cls(
            id=str(body.get("id", "")),
            name=body.get("name") or "",
            type=body.get("type") or "",
        )


class AirtableTable(BaseModel):
    """One table in a base."""

    id: str
    name: str = ""
    primary_field_id: str = ""
    fields: list[AirtableField] = Field(default_factory=list)

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> AirtableTable:
        return cls(
            id=str(body.get("id", "")),
            name=body.get("name") or "",
            primary_field_id=body.get("primaryFieldId") or "",
            fields=[AirtableField.from_api(f) for f in body.get("fields") or []],
        )
