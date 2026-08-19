"""Airtable ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from loom.toolsets.airtable.models import AirtableField, AirtableRecord
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

_SCOPES = ["data.records:read"]
_WRITE_SCOPES = ["data.records:write"]
_SCHEMA_SCOPES = ["schema.bases:read"]

AIRTABLE_MANIFEST = ToolsetManifest(
    id="airtable",
    version="1.0.0",
    summary="Airtable — read, create, update, and delete rows in a base.",
    description=(
        "A personal access token is granted per base, so a valid token for a "
        "base it was not granted returns 403 rather than an empty result.\n\n"
        "A response is keyed by field **name**, not id. Renaming a column in "
        "the UI changes every key, and a workflow reading the old name gets "
        "None rather than an error. An empty field is omitted rather than "
        "nulled, so 'empty' and 'no such column' look identical in a "
        "response — airtable_list_fields is what tells them apart, and it "
        "returns the stable fld… id beside the name.\n\n"
        "Writes are capped at ten records per request and batched for you, but "
        "there is no transaction across batches: a failure partway leaves the "
        "earlier batches written. Creating is not retried, because Airtable "
        "has no idempotency key.\n\n"
        "Five requests per second per base, and exceeding it locks the base "
        "for thirty seconds — so nothing here fans out internally."
    ),
    base_url="https://api.airtable.com/v0",
    auth={"type": "bearer", "fields": ["AIRTABLE_TOKEN", "AIRTABLE_BASE_ID"]},
    tools_module="loom.toolsets.airtable.tools",
    opaque_ids={
        # A 3-character kind prefix and 14 characters of base62.
        r"\brec[A-Za-z0-9]{14}\b": "record",
        r"\bfld[A-Za-z0-9]{14}\b": "field",
    },
    egress_hosts=["api.airtable.com"],
    rate_limits={
        "requests": "5 per second per base; a 429 locks the base for 30 seconds",
        "page": "100 rows maximum per request; reads follow pages for you",
        "batch": "10 records maximum per write; batched for you, with no transaction",
    },
    groups={
        "records": [
            OperationSpec(
                id="records.list",
                function="airtable_list_records",
                summary="Rows from one table, following pages.",
                effect=EffectClass.READ,
                scopes=_SCOPES,
                idempotent=True,
                pagination=True,
                output_schema=AirtableRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.get",
                function="airtable_get_record",
                summary="Fetch one row by rec… id.",
                effect=EffectClass.READ,
                scopes=_SCOPES,
                idempotent=True,
                output_schema=AirtableRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.find",
                function="airtable_find_records",
                summary="Rows where one field exactly equals a value.",
                description=(
                    "The resolver for a row: every write takes a rec… id, and "
                    "a value passed where an id belongs matches nothing and "
                    "reports no error. Matches on a field name, which a rename "
                    "breaks silently."
                ),
                effect=EffectClass.READ,
                scopes=_SCOPES,
                idempotent=True,
                pagination=True,
                resolves="record",
                output_schema=AirtableRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.create",
                function="airtable_create_records",
                summary="Create rows, ten at a time. Not retried.",
                description=(
                    "Airtable has no idempotency key, so a timeout after it "
                    "accepted a batch is indistinguishable from a failure. "
                    "There is no transaction across batches either."
                ),
                effect=EffectClass.WRITE,
                scopes=_WRITE_SCOPES,
                idempotent=False,
                output_schema=AirtableRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.update",
                function="airtable_update_records",
                summary="Patch rows by id; fields not named are left alone.",
                description=(
                    "A patch, not a replace. Airtable's PUT clears every field "
                    "not sent — a data-loss bug that returns 200 — so it is "
                    "not exposed."
                ),
                effect=EffectClass.WRITE,
                scopes=_WRITE_SCOPES,
                idempotent=True,
                output_schema=AirtableRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.delete",
                function="airtable_delete_records",
                summary="Delete rows by id, ten at a time.",
                effect=EffectClass.DESTRUCTIVE,
                scopes=_WRITE_SCOPES,
                idempotent=True,
                reversible=False,
                output_schema={
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The rec… ids actually deleted.",
                },
            ),
        ],
        "schema": [
            OperationSpec(
                id="fields.list",
                function="airtable_list_fields",
                summary="Every column in a table, with its stable id and its name.",
                description=(
                    "The resolver for a field. A response is keyed by name and "
                    "an empty field is omitted rather than nulled, so 'empty' "
                    "and 'gone' are indistinguishable without this."
                ),
                effect=EffectClass.READ,
                scopes=_SCHEMA_SCOPES,
                idempotent=True,
                resolves="field",
                output_schema=AirtableField.model_json_schema(),
            ),
        ],
    },
)
