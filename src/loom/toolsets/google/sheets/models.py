"""Typed rows for the Google Sheets toolset."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["SheetRange", "SheetTab", "Spreadsheet", "UpdateResult"]


class SheetTab(BaseModel):
    """One tab within a spreadsheet."""

    id: int
    """``sheetId``. Stable across renames, unlike ``title``."""

    title: str = ""
    """What the tab is called — and what an A1 range names it by. A rename
    breaks every range string that used the old title."""

    index: int = 0
    row_count: int = 0
    column_count: int = 0

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> SheetTab:
        props = body.get("properties") or body
        grid = props.get("gridProperties") or {}
        return cls(
            id=int(props.get("sheetId") or 0),
            title=props.get("title") or "",
            index=int(props.get("index") or 0),
            row_count=int(grid.get("rowCount") or 0),
            column_count=int(grid.get("columnCount") or 0),
        )


class Spreadsheet(BaseModel):
    """A whole spreadsheet file."""

    id: str
    title: str = ""
    url: str = ""
    tabs: list[SheetTab] = Field(default_factory=list)

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> Spreadsheet:
        props = body.get("properties") or {}
        return cls(
            id=str(body.get("spreadsheetId", "")),
            title=props.get("title") or "",
            url=body.get("spreadsheetUrl") or "",
            tabs=[SheetTab.from_api(s) for s in body.get("sheets") or []],
        )


class SheetRange(BaseModel):
    """Values read out of one range."""

    range: str = ""
    """The range Sheets actually returned, which is not always the one asked
    for — a request past the end of the data comes back trimmed."""

    values: list[list[Any]] = Field(default_factory=list)
    """Rows, each a list of cells.

    **Rows are ragged.** Sheets truncates trailing empty cells, so a row whose
    last two columns are blank comes back short rather than padded — indexing
    column 5 on it raises IndexError. ``rows_padded`` is the safe read."""

    def rows_padded(self, width: int = 0) -> list[list[Any]]:
        """Every row padded to *width*, or to the widest row.

        The failure this avoids is real and quiet: a header row of eight
        columns and a data row whose last three are empty come back as eight
        and five, and ``row[7]`` raises on some rows and not others.
        """
        target = width or max((len(row) for row in self.values), default=0)
        return [list(row) + [""] * (target - len(row)) for row in self.values]

    def as_dicts(self, header_row: int = 0) -> list[dict[str, Any]]:
        """Rows keyed by the header row's labels.

        Padded first, so a short row yields empty strings rather than missing
        keys — a caller doing ``row["Status"]`` should not have to know which
        rows happened to end early.
        """
        padded = self.rows_padded()
        if len(padded) <= header_row:
            return []
        header = [str(cell) for cell in padded[header_row]]
        return [dict(zip(header, row, strict=True)) for row in padded[header_row + 1 :]]

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> SheetRange:
        return cls(
            range=body.get("range") or "",
            values=[list(row) for row in body.get("values") or []],
        )


class UpdateResult(BaseModel):
    """What a write changed."""

    spreadsheet_id: str = ""
    updated_range: str = ""
    updated_rows: int = 0
    updated_cells: int = 0

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> UpdateResult:
        # An append nests its counts under `updates`; an update reports them
        # at the top level. Same numbers, two shapes.
        inner = body.get("updates") or body
        return cls(
            spreadsheet_id=str(body.get("spreadsheetId") or inner.get("spreadsheetId") or ""),
            updated_range=inner.get("updatedRange") or "",
            updated_rows=int(inner.get("updatedRows") or 0),
            updated_cells=int(inner.get("updatedCells") or 0),
        )
