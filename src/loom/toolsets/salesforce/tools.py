"""Salesforce step functions for use inside LOOM workflows.

    from loom.toolsets.salesforce.tools import salesforce_find_accounts

    accounts = await ctx.step(salesforce_find_accounts, name="ACME")

Retries are per operation. Reads retry; **creating a record does not**, because
Salesforce offers no idempotency key and a retry after a timeout files a second
lead that a salesperson then calls. Updates and deletes retry once: naming the
same record id twice reaches the same end state.
"""

from __future__ import annotations

from typing import Any

from loom import Retry, step
from loom.toolsets.pagination import Results
from loom.toolsets.salesforce.client import SalesforceClient
from loom.toolsets.salesforce.models import (
    SalesforceAccount,
    SalesforceContact,
    SalesforceOpportunity,
    SalesforceRecord,
    SalesforceUser,
    SalesforceWriteResult,
)

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


@step(retry=_READ)
async def salesforce_query(soql: str, limit: int = 200) -> Results[SalesforceRecord]:
    """Run a SOQL query, following Salesforce's paging until it says done.

    The power tool: anything the typed finders below do not cover. Use
    ``salesforce_describe_object`` first when the org's custom fields are not
    known, rather than guessing a field name that returns a syntax error.

    Args:
        soql: A SOQL statement, e.g. ``"SELECT Id, Name FROM Account LIMIT 10"``.
        limit: Maximum records to return across all batches. Defaults to 200.

    Returns:
        Paginated records. Check ``.complete`` before reporting a total.
    """
    from loom.toolsets.factory import client_for


    return await (await client_for("salesforce", SalesforceClient)).query(soql, limit=limit)


@step(retry=_READ)
async def salesforce_describe_object(sobject: str) -> dict[str, Any]:
    """Describe an object's fields, types, and which are writable.

    Args:
        sobject: API name, e.g. ``"Account"`` or ``"Deal__c"``.

    Returns:
        The object's name, label, and its list of fields.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("salesforce", SalesforceClient)).describe_object(sobject)


@step(retry=_READ)
async def salesforce_get_record(
    sobject: str, record_id: str, fields: list[str] | None = None
) -> SalesforceRecord:
    """Fetch one record of any object type.

    Args:
        sobject: API name, e.g. ``"Opportunity"``.
        record_id: The 15- or 18-character Salesforce id.
        fields: Field names to return. Omit for all readable fields.

    Returns:
        The record, with its id, type, REST path, and fields.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("salesforce", SalesforceClient)
    return await client.get_record(sobject, record_id, fields=fields)


@step(retry=_UNSAFE_WRITE)
async def salesforce_create_record(
    sobject: str, values: dict[str, Any]
) -> SalesforceWriteResult:
    """Create a record of any object type.

    Not retried: Salesforce has no idempotency key, so a timeout after the
    record was written would create a duplicate.

    Args:
        sobject: API name, e.g. ``"Lead"``.
        values: Field names to values, e.g. ``{"LastName": "Chen", "Company": "ACME"}``.

    Returns:
        The new record's id, and whether Salesforce accepted it.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("salesforce", SalesforceClient)).create_record(sobject, values)


@step(retry=_IDEMPOTENT_WRITE)
async def salesforce_update_record(
    sobject: str, record_id: str, values: dict[str, Any]
) -> SalesforceWriteResult:
    """Update a record. Only the fields you pass are changed.

    Args:
        sobject: API name.
        record_id: The record to update.
        values: Field names to new values.

    Returns:
        The record id and success flag. Salesforce returns no body here, so
        success is reported rather than echoed.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("salesforce", SalesforceClient)
    return await client.update_record(sobject, record_id, values)


@step(retry=_IDEMPOTENT_WRITE)
async def salesforce_delete_record(
    sobject: str, record_id: str
) -> SalesforceWriteResult:
    """Delete a record.

    Args:
        sobject: API name.
        record_id: The record to delete.

    Returns:
        The record id and success flag.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("salesforce", SalesforceClient)
    return await client.delete_record(sobject, record_id)


@step(retry=_READ)
async def salesforce_find_accounts(
    query: str = "", limit: int = 20
) -> Results[SalesforceAccount]:
    """Find accounts by name.

    Resolve a company here before filtering by it: every Salesforce write takes
    an 18-character id, and a company name passed where an id belongs matches
    nothing and reports no error.

    Args:
        query: Substring of the account name. Empty returns the first accounts.
        limit: Maximum accounts. Defaults to 20.

    Returns:
        Accounts with id, name, industry, website, phone, and owner.
    """
    from loom.toolsets.factory import client_for

    client = await client_for("salesforce", SalesforceClient)
    return await client.find_accounts(query, limit=limit)


@step(retry=_READ)
async def salesforce_find_contacts(
    query: str = "", account_id: str = "", limit: int = 20
) -> Results[SalesforceContact]:
    """Find contacts by name or email, optionally within one account.

    Args:
        query: Substring matched against name and email.
        account_id: Restrict to this account's contacts.
        limit: Maximum contacts. Defaults to 20.

    Returns:
        Contacts with id, name, email, phone, title, and account.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("salesforce", SalesforceClient)).find_contacts(
        query, account_id=account_id, limit=limit
    )


@step(retry=_READ)
async def salesforce_find_opportunities(
    query: str = "",
    account_id: str = "",
    stage: str = "",
    open_only: bool = False,
    limit: int = 20,
) -> Results[SalesforceOpportunity]:
    """Find opportunities, newest close date first.

    Args:
        query: Substring of the opportunity name.
        account_id: Restrict to one account.
        stage: Exact stage name, e.g. ``"Negotiation/Review"``.
        open_only: Exclude closed opportunities. Defaults to False.
        limit: Maximum opportunities. Defaults to 20.

    Returns:
        Opportunities with stage, amount, close date, account, and owner.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("salesforce", SalesforceClient)).find_opportunities(
        query, account_id=account_id, stage=stage, open_only=open_only, limit=limit
    )


@step(retry=_READ)
async def salesforce_find_users(query: str = "", limit: int = 20) -> Results[SalesforceUser]:
    """Find Salesforce users by name or email.

    Resolve a person here before assigning an owner: ``OwnerId`` takes an id,
    and a name passed where an id belongs is rejected or silently ignored.

    Args:
        query: Substring matched against name and email.
        limit: Maximum users. Defaults to 20.

    Returns:
        Users with id, name, email, username, and active flag.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("salesforce", SalesforceClient)).find_users(query, limit=limit)


@step(retry=_READ)
async def salesforce_whoami() -> SalesforceUser:
    """Return the user these credentials authenticate as.

    Returns:
        The authenticated user's id, name, email, and username.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("salesforce", SalesforceClient)).whoami()
