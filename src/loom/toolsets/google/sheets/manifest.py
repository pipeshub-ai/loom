"""Google Sheets ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from loom.toolsets.google.sheets.models import (
    SheetRange,
    SheetTab,
    Spreadsheet,
    UpdateResult,
)
from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

GOOGLE_SHEETS_MANIFEST = ToolsetManifest(
    id="google_sheets",
    version="1.0.0",
    summary="Google Sheets — read and write cell values in a spreadsheet.",
    description=(
        "The tracking-sheet toolset. sheets_append_rows is what most workflows "
        "want, and two things about it decide whether it works.\n\n"
        "Append writes after the last row of the **table the range sits in**, "
        "not the last row of the sheet — Sheets locates a table by looking "
        "around the range, so appending to A1 when an unrelated block is at "
        "the top writes into that block. Name the table's own columns.\n\n"
        "valueInputOption decides whether a value is data or input. RAW stores "
        "the string; USER_ENTERED parses it as though typed, which is what "
        "makes =SUM(A1:A9) a formula and what turns 1/2 into a date and a "
        "leading + into an error cell.\n\n"
        "Reads come back **ragged**: Sheets truncates trailing empty cells, so "
        "a header of eight columns and a row whose last three are blank return "
        "eight and five. Use .rows_padded() or .as_dicts() rather than "
        "indexing the raw lists.\n\n"
        "An A1 range names a tab by its title, so renaming a tab breaks every "
        "stored range. sheets_find_tab returns the stable sheetId."
    ),
    provider="loom",
    base_url="https://sheets.googleapis.com/v4/spreadsheets",
    auth=AuthSpec(
        # What *this* toolset needs, which is narrower than the account's.
        # Read from the client's own SCOPES until now, where nothing outside
        # that module could see it — and `build_client` has to, because a
        # service account bakes scopes into the assertion it signs.
        scopes=(
            "https://www.googleapis.com/auth/spreadsheets",
        ),
        client="loom.toolsets.google.sheets.client:SheetsClient",
        credentials="loom.toolsets.google.auth:GoogleAuth",
        # One credential across the five Google toolsets: `GoogleAuth`
        # caches a single token and merges each toolset's scopes into it,
        # so connecting once serves the set — and a second credential
        # would be a second token with a narrower scope set, which is the
        # 403 that reads as a broken credential.
        kind="oauth2",
        credential="google",
        provider="google",
        fields=(
            # Three alternatives, mirroring `GoogleCredentials.mode`. The
            # refresh trio wins over a ready-made access token when both are
            # set — an access token lives about an hour and a refresh token
            # mints them indefinitely.
            AuthField(name="GOOGLE_ACCESS_TOKEN", label="Access token", mode="token"),
            AuthField(name="GOOGLE_CLIENT_ID", label="OAuth client id", secret=False,
                      mode="refresh"),
            AuthField(name="GOOGLE_CLIENT_SECRET", label="OAuth client secret",
                      mode="refresh"),
            AuthField(name="GOOGLE_REFRESH_TOKEN", label="Refresh token", mode="refresh"),
            AuthField(name="GOOGLE_SERVICE_ACCOUNT_FILE", label="Service account JSON",
                      secret=False, mode="service_account"),
            AuthField(name="GOOGLE_IMPERSONATE_SUBJECT", label="User to impersonate",
                      secret=False, required=False),
        ),
        setup_url="https://console.cloud.google.com/apis/credentials",
        docs_url="https://developers.google.com/sheets/api/scopes",
    ),
    tools_module="loom.toolsets.google.sheets.tools",
    egress_hosts=["sheets.googleapis.com", "oauth2.googleapis.com"],
    rate_limits={
        "requests": (
            "300 per minute per project and 60 per minute per user; a burst "
            "of small appends is the usual way to meet it"
        ),
        "cells": "10 million per spreadsheet, across every tab",
    },
    groups={
        "structure": [
            OperationSpec(
                id="spreadsheets.get",
                function="sheets_get_spreadsheet",
                summary="The file and its tabs, without any cell values.",
                effect=EffectClass.READ,
                scopes=_SCOPES,
                idempotent=True,
                output_schema=Spreadsheet.model_json_schema(),
            ),
            OperationSpec(
                id="tabs.find",
                function="sheets_find_tab",
                summary="The tab with an exact title, and its stable sheetId.",
                description=(
                    "The resolver. An A1 range names a tab by title, so a "
                    "rename breaks every stored range and the failure is a 400 "
                    "that reads as a malformed range. Matched exactly — a "
                    "prefix match would return 'Leads Archive' for 'Leads'."
                ),
                effect=EffectClass.READ,
                scopes=_SCOPES,
                idempotent=True,
                resolves="tab",
                output_schema=SheetTab.model_json_schema(),
            ),
        ],
        "values": [
            OperationSpec(
                id="values.read",
                function="sheets_read_range",
                summary="Values from one A1 range. Rows come back ragged.",
                description=(
                    "Sheets truncates trailing empty cells, so rows are not "
                    "all the same length. .rows_padded() and .as_dicts() are "
                    "the safe reads."
                ),
                effect=EffectClass.READ,
                scopes=_SCOPES,
                idempotent=True,
                output_schema=SheetRange.model_json_schema(),
            ),
            OperationSpec(
                id="values.append",
                function="sheets_append_rows",
                summary="Add rows after the last row of a table. Not retried.",
                description=(
                    "No idempotency key, so a retry after a timeout writes the "
                    "rows twice. Appends to the table the range sits in, not "
                    "the sheet."
                ),
                effect=EffectClass.WRITE,
                scopes=_SCOPES,
                idempotent=False,
                output_schema=UpdateResult.model_json_schema(),
            ),
            OperationSpec(
                id="values.update",
                function="sheets_update_range",
                summary="Overwrite the cells in one range.",
                description=(
                    "Retried, unlike an append: writing the same rectangle "
                    "twice reaches the same end state."
                ),
                effect=EffectClass.WRITE,
                scopes=_SCOPES,
                idempotent=True,
                output_schema=UpdateResult.model_json_schema(),
            ),
            OperationSpec(
                id="values.clear",
                function="sheets_clear_range",
                summary="Empty the cells in one range, leaving formatting.",
                description=(
                    "Irreversible through this API — Sheets' undo history is a "
                    "UI feature, not an endpoint."
                ),
                effect=EffectClass.DESTRUCTIVE,
                scopes=_SCOPES,
                idempotent=True,
                reversible=False,
                output_schema={
                    "type": "string",
                    "description": "The range that was cleared, as Sheets reports it.",
                },
            ),
        ],
    },
)
