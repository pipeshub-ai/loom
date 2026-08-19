"""Airtable step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.airtable.tools import airtable_find_records

    rows = await ctx.step(airtable_find_records, "Leads", "Email", "ada@example.com")

**Everything here is keyed by field name**, which is what Airtable returns and
what a rename silently breaks. ``airtable_list_fields`` is the resolver:
it gives both the stable ``fld…`` id and the current name, so a workflow that
must survive a rename can address columns by id instead.

Creating rows is **not retried**. Airtable has no idempotency key, so a timeout
after it accepted a batch is indistinguishable from a failure and a retry
writes the rows twice. Updates and deletes do retry: naming the same record id
twice reaches the same end state.

One rate limit worth planning around: **five requests per second per base**,
and exceeding it locks the base out for thirty seconds. Reads follow pages one
request at a time for that reason, and nothing here fans out internally.
"""

from __future__ import annotations

from typing import Any

from loom import Retry, step
from loom.toolsets.airtable.client import get_default_client
from loom.toolsets.airtable.models import AirtableField, AirtableRecord
from loom.toolsets.pagination import Results

#: Reads. A 429 is retryable but expensive — Airtable locks the base for 30
#: seconds — so the backoff starts wide rather than tight.
_READ = Retry(max_attempts=3, initial_delay=2.0)

#: Creates. No idempotency key, so a retry writes the rows twice.
_NO_RETRY = Retry(max_attempts=1)

#: Updates and deletes. Naming the same record id twice reaches the same end
#: state, so a retry is safe.
_IDEMPOTENT = Retry(max_attempts=2, initial_delay=2.0)


@step(retry=_READ)
async def airtable_list_records(
    table: str,
    formula: str = "",
    view: str = "",
    sort_field: str = "",
    sort_desc: bool = False,
    return_field_ids: bool = False,
    base_id: str = "",
    limit: int = 100,
) -> Results[AirtableRecord]:
    """Rows from one table, following pages.

    Args:
        table: Table name or ``tbl…`` id. An id survives a rename; a name does
            not, and a renamed table is a 404.
        formula: An Airtable ``filterByFormula``, e.g. ``{Status} = 'Ready'``.
            Quote literals with ``airtable_escape`` semantics — an apostrophe
            terminates the literal.
        view: Only rows visible in this view, in the view's own order.
        sort_field: Field **name** to sort by.
        sort_desc: Sort descending rather than ascending.
        return_field_ids: Key the returned ``fields`` by ``fld…`` id instead of
            name. Stable across renames, and unreadable to a person — pick one.
        base_id: Override the default base.
        limit: Most rows to return, following pages as needed.
    """
    return await get_default_client().list_records(
        table,
        base_id=base_id,
        formula=formula,
        view=view,
        sort_field=sort_field,
        sort_desc=sort_desc,
        return_field_ids=return_field_ids,
        limit=limit,
    )


@step(retry=_READ)
async def airtable_get_record(
    table: str, record_id: str, base_id: str = ""
) -> AirtableRecord:
    """Fetch one row by id.

    Args:
        table: Table name or ``tbl…`` id.
        record_id: A ``rec…`` id.
        base_id: Override the default base.
    """
    return await get_default_client().get_record(table, record_id, base_id=base_id)


@step(retry=_READ)
async def airtable_find_records(
    table: str, field: str, value: str, base_id: str = "", limit: int = 10
) -> Results[AirtableRecord]:
    """Rows where one field exactly equals a value.

    The resolver for a row: every write takes a ``rec…`` id, and a value passed
    where an id belongs matches nothing and reports no error.

    Args:
        table: Table name or ``tbl…`` id.
        field: Field **name** to match on. A rename breaks this silently —
            ``airtable_list_fields`` is how a workflow survives one.
        value: The exact value. Apostrophes are escaped for you.
        base_id: Override the default base.
        limit: Most rows to return.
    """
    return await get_default_client().find_records(
        table, field, value, base_id=base_id, limit=limit
    )


@step(retry=_READ)
async def airtable_list_fields(table: str, base_id: str = "") -> list[AirtableField]:
    """Every column in a table, with both its stable id and its current name.

    The resolver for a *field*. Airtable keys a response by name and omits an
    empty field rather than nulling it, so "is this column empty or gone?" is a
    question only this can answer.

    Args:
        table: Table name or ``tbl…`` id.
        base_id: Override the default base.
    """
    return await get_default_client().list_fields(table, base_id=base_id)


@step(retry=_NO_RETRY)
async def airtable_create_records(
    table: str,
    rows: list[dict[str, Any]],
    typecast: bool = False,
    base_id: str = "",
) -> list[AirtableRecord]:
    """Create rows, ten at a time.

    **Not retried.** Airtable has no idempotency key, so a timeout after it
    accepted a batch is indistinguishable from a failure and a retry writes the
    rows twice.

    Batching is done for you because the ten-record cap is Airtable's, not the
    workflow's — but there is no transaction across batches, so a failure
    partway leaves the earlier batches written.

    Args:
        table: Table name or ``tbl…`` id.
        rows: One dict of field name to value per row.
        typecast: Let Airtable coerce values — creating a select option that
            does not exist yet, parsing a date from a string. Off by default,
            because coercion turns a typo into a new option rather than an
            error.
        base_id: Override the default base.
    """
    return await get_default_client().create_records(
        table, rows, base_id=base_id, typecast=typecast
    )


@step(retry=_IDEMPOTENT)
async def airtable_update_records(
    table: str,
    updates: list[tuple[str, dict[str, Any]]],
    typecast: bool = False,
    base_id: str = "",
) -> list[AirtableRecord]:
    """Patch rows by id, ten at a time. Fields not named are left alone.

    A patch, not a replace. Airtable's ``PUT`` clears every field not sent,
    which is a data-loss bug that returns 200, so it is not offered here.

    Args:
        table: Table name or ``tbl…`` id.
        updates: ``(record_id, {field: value})`` pairs.
        typecast: Let Airtable coerce values. See ``airtable_create_records``.
        base_id: Override the default base.
    """
    return await get_default_client().update_records(
        table, updates, base_id=base_id, typecast=typecast
    )


@step(retry=_IDEMPOTENT)
async def airtable_delete_records(
    table: str, record_ids: list[str], base_id: str = ""
) -> list[str]:
    """Delete rows by id, ten at a time. Returns the ids deleted.

    Args:
        table: Table name or ``tbl…`` id.
        record_ids: ``rec…`` ids.
        base_id: Override the default base.
    """
    return await get_default_client().delete_records(
        table, record_ids, base_id=base_id
    )
