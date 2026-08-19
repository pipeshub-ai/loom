"""Google Sheets step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.google.sheets.tools import sheets_append_rows

    await ctx.step(sheets_append_rows, sheet_id, "Log!A:D", [[date, name, "sent", ref]])

The tracking-sheet shape most workflows want is ``sheets_append_rows``, and the
two things to get right about it are in its docstring: which table it appends
to, and whether the values are parsed.

**Appending is not retried.** Sheets has no idempotency key, so a timeout after
it accepted the rows is indistinguishable from a failure and a retry writes
them twice. An *update* to a named range does retry — writing the same
rectangle twice reaches the same end state, which is the difference.
"""

from __future__ import annotations

from typing import Any

from loom import Retry, step
from loom.toolsets.google.sheets.client import RAW, get_default_client
from loom.toolsets.google.sheets.models import (
    SheetRange,
    SheetTab,
    Spreadsheet,
    UpdateResult,
)

_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Appends. No idempotency key, so a retry adds the rows a second time.
_NO_RETRY = Retry(max_attempts=1)

#: Writes to a named rectangle. Writing it twice reaches the same end state.
_IDEMPOTENT = Retry(max_attempts=2, initial_delay=1.0)


@step(retry=_READ)
async def sheets_get_spreadsheet(spreadsheet_id: str) -> Spreadsheet:
    """The file and its tabs, without any cell values.

    Args:
        spreadsheet_id: The long id out of the sheet's URL.
    """
    return await get_default_client().get_spreadsheet(spreadsheet_id)


@step(retry=_READ)
async def sheets_find_tab(spreadsheet_id: str, title: str) -> SheetTab | None:
    """The tab with this exact title, or ``None``.

    The resolver. An A1 range names a tab by its **title**, so renaming the tab
    breaks every stored range string — and the failure is a 400 that reads as a
    malformed range. The ``sheetId`` this returns does not change.

    Matched exactly: a prefix match would return "Leads Archive" for "Leads",
    and writing to the wrong tab is worse than finding nothing.

    Args:
        spreadsheet_id: The long id out of the sheet's URL.
        title: The tab's exact name.
    """
    return await get_default_client().find_tab(spreadsheet_id, title)


@step(retry=_READ)
async def sheets_read_range(
    spreadsheet_id: str, range_a1: str, major_dimension: str = "ROWS"
) -> SheetRange:
    """Values from one A1 range.

    **Rows come back ragged.** Sheets truncates trailing empty cells, so a
    header of eight columns and a row whose last three are blank return eight
    and five. Use ``.rows_padded()`` or ``.as_dicts()`` rather than indexing
    the raw lists, or column 7 will raise on some rows and not others.

    Args:
        spreadsheet_id: The long id out of the sheet's URL.
        range_a1: e.g. ``Log!A1:D100``, or ``Log!A:D`` for whole columns.
        major_dimension: ``ROWS`` (default) or ``COLUMNS``.
    """
    return await get_default_client().read_range(
        spreadsheet_id, range_a1, major_dimension=major_dimension
    )


@step(retry=_NO_RETRY)
async def sheets_append_rows(
    spreadsheet_id: str,
    range_a1: str,
    rows: list[list[Any]],
    value_input: str = RAW,
) -> UpdateResult:
    """Add rows after the last row of the table *range_a1* sits in.

    **Not retried** — Sheets has no idempotency key, so a retry after a timeout
    writes the rows twice.

    Two things to get right:

    *Which table.* Sheets locates one by looking around the range given, not by
    scanning the whole sheet. Appending to ``A1`` when an unrelated block sits
    at the top writes into that block. Name the table's own columns —
    ``Log!A:D``.

    *Whether values are parsed.* ``RAW`` (the default here) stores exactly what
    is sent. ``USER_ENTERED`` parses it as though typed, which is what makes
    ``=SUM(A1:A9)`` a formula — and what turns ``1/2`` into a date and a
    leading ``+`` into an error cell.

    Args:
        spreadsheet_id: The long id out of the sheet's URL.
        range_a1: The table to append to, e.g. ``Log!A:D``.
        rows: One list of cell values per row.
        value_input: ``RAW`` or ``USER_ENTERED``.
    """
    return await get_default_client().append_rows(
        spreadsheet_id, range_a1, rows, value_input=value_input
    )


@step(retry=_IDEMPOTENT)
async def sheets_update_range(
    spreadsheet_id: str,
    range_a1: str,
    rows: list[list[Any]],
    value_input: str = RAW,
) -> UpdateResult:
    """Overwrite the cells in one range.

    Retried, unlike an append: writing the same rectangle twice reaches the
    same end state. Cells the payload does not cover are left alone.

    Args:
        spreadsheet_id: The long id out of the sheet's URL.
        range_a1: e.g. ``Log!B2:C4``.
        rows: One list of cell values per row.
        value_input: ``RAW`` or ``USER_ENTERED`` — see ``sheets_append_rows``.
    """
    return await get_default_client().update_range(
        spreadsheet_id, range_a1, rows, value_input=value_input
    )


@step(retry=_IDEMPOTENT)
async def sheets_clear_range(spreadsheet_id: str, range_a1: str) -> str:
    """Empty the cells in one range, leaving formatting alone.

    Irreversible through this API: Sheets' undo history is a UI feature, not an
    endpoint.

    Args:
        spreadsheet_id: The long id out of the sheet's URL.
        range_a1: e.g. ``Log!A2:D``.
    """
    return await get_default_client().clear_range(spreadsheet_id, range_a1)
