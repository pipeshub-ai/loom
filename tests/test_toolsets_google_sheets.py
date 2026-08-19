"""Google Sheets toolset — the traps this API sets, pinned.

Four of them, and all four are silent wrong answers rather than errors: ragged
rows, a value-input option that decides whether text becomes a formula, an
append that targets a *table* rather than a sheet, and a tab named in a range
by a title that can be renamed.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.testing.conformance import verify_effect_profile
from loom.toolsets.google.sheets.client import (
    RAW,
    USER_ENTERED,
    SheetsClient,
)
from loom.toolsets.google.sheets.manifest import GOOGLE_SHEETS_MANIFEST
from loom.toolsets.google.sheets.models import SheetRange, Spreadsheet, UpdateResult

SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz"


class Recorded:
    def __init__(self, method: str, url: str, params: Any, payload: Any):
        self.method = method
        self.url = url
        self.params = dict(params or {})
        self.payload = payload


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the session's request method, so no auth is involved.

    Deliberately at the ``GoogleSession`` seam rather than at httpx: the shared
    auth layer is tested in its own suite, and re-testing it here would make
    these cases fail for reasons that have nothing to do with Sheets.
    """
    from loom.toolsets.google.http import GoogleSession

    calls: list[Recorded] = []
    queue: list[Any] = []

    async def request(
        self: Any,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> Any:
        calls.append(Recorded(method, path, params, json))
        return queue.pop(0) if queue else {}

    monkeypatch.setattr(GoogleSession, "request", request)

    class Wire:
        def __init__(self) -> None:
            self.calls = calls

        def reply(self, body: Any) -> None:
            queue.append(body)

        @property
        def last(self) -> Recorded:
            return calls[-1]

    return Wire()


def client() -> SheetsClient:
    from loom.toolsets.google.auth import GoogleAuth, GoogleCredentials

    return SheetsClient(auth=GoogleAuth(GoogleCredentials(access_token="test-token")))


# ---------------------------------------------------------------------------
# Ragged rows — the trap that raises on some rows and not others
# ---------------------------------------------------------------------------


class TestRaggedRows:
    def test_sheets_truncates_trailing_empty_cells(self) -> None:
        """The shape a real response has, and the reason for `rows_padded`."""
        found = SheetRange.from_api(
            {"range": "Log!A1:C3", "values": [["a", "b", "c"], ["1", "2"], ["x"]]}
        )
        assert [len(row) for row in found.values] == [3, 2, 1]

    def test_padding_squares_them_off(self) -> None:
        found = SheetRange(values=[["a", "b", "c"], ["1", "2"], ["x"]])
        assert found.rows_padded() == [
            ["a", "b", "c"],
            ["1", "2", ""],
            ["x", "", ""],
        ]

    def test_padding_to_an_explicit_width(self) -> None:
        found = SheetRange(values=[["a"]])
        assert found.rows_padded(3) == [["a", "", ""]]

    def test_as_dicts_pads_before_zipping(self) -> None:
        """A caller doing ``row["Status"]`` should not have to know which rows
        happened to end early."""
        found = SheetRange(values=[["Name", "Email", "Status"], ["Ada", "a@b.test"]])
        assert found.as_dicts() == [
            {"Name": "Ada", "Email": "a@b.test", "Status": ""}
        ]

    def test_as_dicts_on_an_empty_range_is_empty(self) -> None:
        assert SheetRange(values=[]).as_dicts() == []

    def test_as_dicts_with_only_a_header_is_empty(self) -> None:
        assert SheetRange(values=[["Name"]]).as_dicts() == []


# ---------------------------------------------------------------------------
# Writes — the option that decides whether text becomes a formula
# ---------------------------------------------------------------------------


class TestWrites:
    @pytest.mark.asyncio()
    async def test_raw_is_the_default(self, wire: Any) -> None:
        """USER_ENTERED turns ``1/2`` into a date and ``+44`` into an error
        cell, so storing what was sent is the safer default."""
        wire.reply({})
        await client().append_rows(SHEET, "Log!A:D", [["a"]])
        assert wire.last.params["valueInputOption"] == RAW

    @pytest.mark.asyncio()
    async def test_user_entered_can_be_asked_for(self, wire: Any) -> None:
        """The only way to write a working formula."""
        wire.reply({})
        await client().append_rows(
            SHEET, "Log!A:D", [["=SUM(A1:A9)"]], value_input=USER_ENTERED
        )
        assert wire.last.params["valueInputOption"] == USER_ENTERED

    @pytest.mark.asyncio()
    async def test_append_inserts_rather_than_overwriting(self, wire: Any) -> None:
        """Sheets' default for append is OVERWRITE, which writes over whatever
        sits below the table."""
        wire.reply({})
        await client().append_rows(SHEET, "Log!A:D", [["a"]])
        assert wire.last.params["insertDataOption"] == "INSERT_ROWS"

    @pytest.mark.asyncio()
    async def test_an_update_is_a_put_to_the_range(self, wire: Any) -> None:
        wire.reply({})
        await client().update_range(SHEET, "Log!B2:C2", [["x", "y"]])
        assert wire.last.method == "PUT"
        assert "Log!B2:C2" in wire.last.url

    @pytest.mark.asyncio()
    async def test_clear_returns_what_it_cleared(self, wire: Any) -> None:
        wire.reply({"clearedRange": "Log!A2:D99"})
        assert await client().clear_range(SHEET, "Log!A2:D") == "Log!A2:D99"


# ---------------------------------------------------------------------------
# Two response shapes for the same numbers
# ---------------------------------------------------------------------------


class TestUpdateResult:
    def test_an_append_nests_its_counts(self) -> None:
        """Append reports under ``updates``; update reports at the top level.

        Same numbers, two shapes — and a reader of one would silently get
        zeroes from the other.
        """
        result = UpdateResult.from_api(
            {
                "spreadsheetId": SHEET,
                "updates": {
                    "updatedRange": "Log!A5:D5",
                    "updatedRows": 1,
                    "updatedCells": 4,
                },
            }
        )
        assert result.updated_rows == 1
        assert result.updated_cells == 4
        assert result.updated_range == "Log!A5:D5"

    def test_an_update_reports_at_the_top_level(self) -> None:
        result = UpdateResult.from_api(
            {
                "spreadsheetId": SHEET,
                "updatedRange": "Log!B2:C2",
                "updatedRows": 1,
                "updatedCells": 2,
            }
        )
        assert result.updated_rows == 1
        assert result.updated_cells == 2


# ---------------------------------------------------------------------------
# Tabs — a title in a range, and a stable id beside it
# ---------------------------------------------------------------------------


class TestTabs:
    def _spreadsheet(self) -> dict[str, Any]:
        return {
            "spreadsheetId": SHEET,
            "spreadsheetUrl": "https://docs.google.test/x",
            "properties": {"title": "Outreach"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Log",
                        "index": 0,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26},
                    }
                },
                {"properties": {"sheetId": 12345, "title": "Log Archive", "index": 1}},
            ],
        }

    @pytest.mark.asyncio()
    async def test_it_finds_a_tab_by_exact_title(self, wire: Any) -> None:
        wire.reply(self._spreadsheet())
        tab = await client().find_tab(SHEET, "Log")
        assert tab is not None
        assert tab.id == 0
        assert tab.row_count == 1000

    @pytest.mark.asyncio()
    async def test_it_does_not_prefix_match(self, wire: Any) -> None:
        """A prefix match would return "Log Archive" for "Log", and writing to
        the wrong tab is worse than finding nothing."""
        wire.reply(self._spreadsheet())
        assert await client().find_tab(SHEET, "Lo") is None

    @pytest.mark.asyncio()
    async def test_a_missing_tab_is_none_not_an_error(self, wire: Any) -> None:
        wire.reply(self._spreadsheet())
        assert await client().find_tab(SHEET, "Nope") is None

    def test_the_spreadsheet_model_reads_nested_properties(self) -> None:
        sheet = Spreadsheet.from_api(self._spreadsheet())
        assert sheet.title == "Outreach"
        assert [t.title for t in sheet.tabs] == ["Log", "Log Archive"]


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_effects_are_consistent_with_the_client(self) -> None:
        from pathlib import Path

        from loom.toolsets.google.sheets import tools

        source = Path("src/loom/toolsets/google/sheets/client.py").read_text(
            encoding="utf-8"
        )
        verify_effect_profile(
            GOOGLE_SHEETS_MANIFEST, tools_module=tools, client_source=source
        )

    def test_appending_is_declared_non_idempotent(self) -> None:
        by_id = {op.id: op for op in GOOGLE_SHEETS_MANIFEST.all_operations()}
        assert by_id["values.append"].idempotent is False
        assert by_id["values.update"].idempotent is True

    def test_clearing_is_destructive_and_irreversible(self) -> None:
        by_id = {op.id: op for op in GOOGLE_SHEETS_MANIFEST.all_operations()}
        assert by_id["values.clear"].effect.value == "destructive"
        assert by_id["values.clear"].reversible is False

    def test_the_tab_resolver_is_declared(self) -> None:
        assert "tab" in GOOGLE_SHEETS_MANIFEST.resolvers()

    def test_it_is_a_separately_grantable_google_toolset(self) -> None:
        """A workflow appending to a tracking sheet has no business holding a
        mail-send scope — the same reason the package ships four rather than
        one."""
        from loom.toolsets.google import GOOGLE_MANIFESTS

        assert GOOGLE_SHEETS_MANIFEST in GOOGLE_MANIFESTS
        assert GOOGLE_SHEETS_MANIFEST.id == "google_sheets"

    def test_every_operation_declares_an_effect(self) -> None:
        for op in GOOGLE_SHEETS_MANIFEST.all_operations():
            assert "effect" in op.model_fields_set, f"{op.id} did not declare one"
