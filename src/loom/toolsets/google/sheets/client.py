"""Google Sheets API v4 client.

Pure httpx, over the shared ``toolsets/google`` auth layer — one cached token
serves Sheets alongside Gmail, Calendar, Drive and Meet.

Four things about this API are silent wrong answers rather than errors.

**Rows are ragged.** The reference is explicit — *"Empty trailing rows and
columns are omitted"* — so a header row of eight columns and a data row whose
last three are blank come back as eight and five. Indexing column 7 raises on
some rows and not others, which is why :meth:`SheetRange.rows_padded` exists
and why nothing here hands back the raw lists without saying so.

**``valueInputOption`` decides whether a write is data or a formula.** The
reference: ``RAW`` means *"the input is not parsed and is inserted as a string
— the input '=1+2' places the string, not the formula"*, while
``USER_ENTERED`` means *"the input is parsed exactly as if it were entered into
the Sheets UI — 'Mar 1 2016' becomes a date, and '=1+2' becomes a formula"*.
This client requires the choice at every write because the wrong one corrupts
data that looks fine in the response.

**A tab is named in an A1 range by its title.** Renaming the tab breaks every
stored range string, and the failure is a 400 that reads as a malformed range.
``find_tab`` is marked ``resolves="tab"`` and returns the stable ``sheetId``
beside the title.

**Append writes after the last row of a *table*, not the sheet.** Sheets finds
the table by looking around the range given, so appending to ``A1`` when there
is an unrelated block at the top of the sheet writes into the wrong place. The
range should name the table's own columns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom.toolsets.google.http import DEFAULT_TIMEOUT, GoogleSession
from loom.toolsets.google.sheets.models import (
    SheetRange,
    SheetTab,
    Spreadsheet,
    UpdateResult,
)

if TYPE_CHECKING:
    import httpx

    from loom.toolsets.google.auth import GoogleAuth

API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

#: Read and write values plus structure. ``spreadsheets.readonly`` exists but a
#: toolset that can only read is a different grant, and the four-toolset split
#: in this package is how that is expressed — not by narrowing this one.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

#: How a written value is interpreted.
#:
#: ``RAW`` stores the string. ``USER_ENTERED`` parses it as typed input, which
#: is what makes ``=SUM(...)`` a formula — and what turns ``1/2`` into a date
#: and ``+44 20`` into an error cell.
RAW = "RAW"
USER_ENTERED = "USER_ENTERED"


class SheetsClient:
    """Thin async wrapper around the Google Sheets API."""

    def __init__(
        self,
        auth: GoogleAuth | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        from loom.toolsets.google.auth import get_default_auth

        resolved = auth or get_default_auth(SCOPES)
        self._session = GoogleSession(
            resolved, API_BASE, transport=transport, timeout=timeout
        )

    # -- structure ----------------------------------------------------------

    async def get_spreadsheet(self, spreadsheet_id: str) -> Spreadsheet:
        """The file and its tabs, without any cell values."""
        body = await self._session.request(
            "GET",
            f"/{spreadsheet_id}",
            params={"fields": "spreadsheetId,spreadsheetUrl,properties.title,sheets.properties"},
        )
        return Spreadsheet.from_api(body or {})

    async def find_tab(self, spreadsheet_id: str, title: str) -> SheetTab | None:
        """The tab with this exact title, or ``None``.

        The resolver. An A1 range names a tab by title, so a rename breaks
        every stored range — the ``sheetId`` this returns does not change.
        Matched exactly: a prefix match would return "Leads Archive" for
        "Leads", and writing to the wrong tab is worse than finding nothing.
        """
        sheet = await self.get_spreadsheet(spreadsheet_id)
        for tab in sheet.tabs:
            if tab.title == title:
                return tab
        return None

    # -- values -------------------------------------------------------------

    async def read_range(
        self, spreadsheet_id: str, range_a1: str, *, major_dimension: str = "ROWS"
    ) -> SheetRange:
        """Values from one A1 range.

        Rows come back ragged — see :meth:`SheetRange.rows_padded`.
        """
        body = await self._session.request(
            "GET",
            f"/{spreadsheet_id}/values/{range_a1}",
            params={"majorDimension": major_dimension},
        )
        return SheetRange.from_api(body or {})

    async def append_rows(
        self,
        spreadsheet_id: str,
        range_a1: str,
        rows: list[list[Any]],
        *,
        value_input: str = RAW,
    ) -> UpdateResult:
        """Add rows after the last row of the table *range_a1* sits in.

        Not after the last row of the *sheet*: Sheets locates a table by
        looking around the range, so appending to ``A1`` when an unrelated
        block sits at the top writes into that block instead. Name the table's
        own columns.

        ``insertDataOption=INSERT_ROWS`` is always sent so an append never
        overwrites whatever sits below the table. The reference documents the
        two options — ``OVERWRITE`` "overwrites existing data in the areas it
        is written", ``INSERT_ROWS`` inserts — but states no default, so the
        safe one is named explicitly rather than assumed.
        """
        body = await self._session.request(
            "POST",
            f"/{spreadsheet_id}/values/{range_a1}:append",
            params={
                "valueInputOption": value_input,
                "insertDataOption": "INSERT_ROWS",
            },
            json={"values": rows},
        )
        return UpdateResult.from_api(body or {})

    async def update_range(
        self,
        spreadsheet_id: str,
        range_a1: str,
        rows: list[list[Any]],
        *,
        value_input: str = RAW,
    ) -> UpdateResult:
        """Overwrite the cells in *range_a1*.

        Cells the payload does not cover are left alone — this writes a
        rectangle, it does not clear the rest of the sheet.
        """
        body = await self._session.request(
            "PUT",
            f"/{spreadsheet_id}/values/{range_a1}",
            params={"valueInputOption": value_input},
            json={"values": rows},
        )
        return UpdateResult.from_api(body or {})

    async def clear_range(self, spreadsheet_id: str, range_a1: str) -> str:
        """Empty the cells in *range_a1*, leaving formatting alone.

        Returns the range cleared. Destructive and irreversible through this
        API — Sheets' own undo history is a UI feature, not an endpoint.
        """
        body = await self._session.request(
            "POST", f"/{spreadsheet_id}/values/{range_a1}:clear", json={}
        )
        return str((body or {}).get("clearedRange") or "")



