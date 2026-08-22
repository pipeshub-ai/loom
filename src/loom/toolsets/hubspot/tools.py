"""HubSpot step functions for use inside LOOM workflows.

    from loom.toolsets.hubspot.tools import hubspot_find_contacts

    people = await ctx.step(hubspot_find_contacts, query="acme.com")

Retries are per operation. Reads retry; **creates do not**, because HubSpot has
no idempotency key and a retry after a timeout files a duplicate contact that a
salesperson then calls. Updates and archives retry once: naming the same record
twice reaches the same end state.
"""

from __future__ import annotations

from typing import Any

from loom import Retry, step
from loom.toolsets.hubspot.client import HubSpotClient
from loom.toolsets.hubspot.models import (
    HubSpotAccount,
    HubSpotCompany,
    HubSpotContact,
    HubSpotDeal,
    HubSpotObject,
    HubSpotOwner,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


@step(retry=_READ)
async def hubspot_list_objects(
    object_type: str,
    limit: int = 50,
    properties: list[str] | None = None,
    archived: bool = False,
) -> Results[HubSpotObject]:
    """List records of any CRM object type, newest id first.

    Args:
        object_type: ``"contacts"``, ``"companies"``, ``"deals"``, ``"tickets"``,
            or a custom object's type name.
        limit: Maximum records across all pages. Defaults to 50.
        properties: Property names to return. Omit for a sensible default set.
        archived: Return archived records instead of active ones.

    Returns:
        Paginated records. Check ``.complete`` before reporting a total.
    """
    from loom.toolsets.factory import client_for


    return await (await client_for("hubspot", HubSpotClient)).list_objects(
        object_type, limit=limit, properties=properties, archived=archived
    )


@step(retry=_READ)
async def hubspot_search_objects(
    object_type: str,
    query: str = "",
    filters: list[dict[str, Any]] | None = None,
    sorts: list[dict[str, str]] | None = None,
    properties: list[str] | None = None,
    limit: int = 50,
) -> Results[HubSpotObject]:
    """Search one CRM object type by text or by property filters.

    HubSpot caps any one search at 10,000 total results; this stops there and
    reports ``.complete`` as False rather than paging into the 400 that lies
    just past the ceiling. Search is also rate limited to five requests a
    second per account, so a large paged search will be slower than a list.

    Args:
        object_type: ``"contacts"``, ``"companies"``, ``"deals"``, ``"tickets"``.
        query: Free text matched across default searchable properties.
        filters: Filter dicts, e.g.
            ``[{"propertyName": "email", "operator": "EQ", "value": "a@b.com"}]``.
            Operators: EQ, NEQ, LT, LTE, GT, GTE, BETWEEN, IN, NOT_IN,
            HAS_PROPERTY, NOT_HAS_PROPERTY, CONTAINS_TOKEN, NOT_CONTAINS_TOKEN.
        sorts: e.g. ``[{"propertyName": "createdate", "direction": "DESCENDING"}]``.
        properties: Property names to return.
        limit: Maximum records. Defaults to 50.

    Returns:
        Paginated matching records.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).search_objects(
        object_type,
        query=query,
        filters=filters,
        sorts=sorts,
        properties=properties,
        limit=limit,
    )


@step(retry=_READ)
async def hubspot_get_object(
    object_type: str,
    object_id: str,
    properties: list[str] | None = None,
    id_property: str = "",
) -> HubSpotObject:
    """Fetch one record by id, or by another unique property.

    Args:
        object_type: The object type.
        object_id: The record id, or the value of ``id_property``.
        properties: Property names to return.
        id_property: Look up by this property instead of the record id,
            e.g. ``"email"``.

    Returns:
        The record.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).get_object(
        object_type, object_id, properties=properties, id_property=id_property
    )


@step(retry=_UNSAFE_WRITE)
async def hubspot_create_object(
    object_type: str, properties: dict[str, Any]
) -> HubSpotObject:
    """Create a record of any CRM object type.

    Not retried: HubSpot has no idempotency key, so a timeout after the record
    was written would create a duplicate.

    Args:
        object_type: The object type.
        properties: Property names to values, e.g. ``{"email": "a@b.com"}``.

    Returns:
        The created record, including its id.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).create_object(object_type, properties)


@step(retry=_IDEMPOTENT_WRITE)
async def hubspot_update_object(
    object_type: str, object_id: str, properties: dict[str, Any]
) -> HubSpotObject:
    """Update a record. Only the properties you pass are changed.

    Args:
        object_type: The object type.
        object_id: The record to update.
        properties: Property names to new values.

    Returns:
        The updated record.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).update_object(
        object_type, object_id, properties
    )


@step(retry=_IDEMPOTENT_WRITE)
async def hubspot_archive_object(object_type: str, object_id: str) -> bool:
    """Archive a record. HubSpot archives rather than hard-deleting.

    Args:
        object_type: The object type.
        object_id: The record to archive.

    Returns:
        True once HubSpot has accepted it.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).archive_object(object_type, object_id)


@step(retry=_READ)
async def hubspot_find_contacts(query: str = "", limit: int = 20) -> Results[HubSpotContact]:
    """Find contacts by free text — name, email, or company.

    Resolve a person here before filtering or associating: HubSpot writes take
    record ids, and a name passed where an id belongs matches nothing and
    reports no error.

    Args:
        query: Free text matched across searchable contact properties.
        limit: Maximum contacts. Defaults to 20.

    Returns:
        Contacts with email, name, company, phone, and lifecycle stage.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).find_contacts(query, limit=limit)


@step(retry=_READ)
async def hubspot_get_contact_by_email(email: str) -> HubSpotContact:
    """Fetch one contact by email address.

    The direct route, for a workflow that starts from an inbox: searching for
    an address returns a list a caller then has to disambiguate.

    Args:
        email: The contact's email address.

    Returns:
        The contact.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).get_contact_by_email(email)


@step(retry=_UNSAFE_WRITE)
async def hubspot_create_contact(properties: dict[str, Any]) -> HubSpotContact:
    """Create a contact.

    Not retried: no idempotency key, so a retry files a duplicate person.

    Args:
        properties: e.g. ``{"email": "a@b.com", "firstname": "Ada"}``.

    Returns:
        The created contact.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).create_contact(properties)


@step(retry=_READ)
async def hubspot_find_companies(
    query: str = "", limit: int = 20
) -> Results[HubSpotCompany]:
    """Find companies by free text — name or domain.

    Args:
        query: Free text matched across searchable company properties.
        limit: Maximum companies. Defaults to 20.

    Returns:
        Companies with name, domain, industry, and location.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).find_companies(query, limit=limit)


@step(retry=_READ)
async def hubspot_find_deals(query: str = "", limit: int = 20) -> Results[HubSpotDeal]:
    """Find deals by free text.

    Args:
        query: Free text matched across searchable deal properties.
        limit: Maximum deals. Defaults to 20.

    Returns:
        Deals with stage, pipeline, amount, and close date.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).find_deals(query, limit=limit)


@step(retry=_UNSAFE_WRITE)
async def hubspot_create_deal(properties: dict[str, Any]) -> HubSpotDeal:
    """Create a deal.

    Not retried: no idempotency key, so a retry files a duplicate deal that
    shows up twice in the forecast.

    Args:
        properties: e.g. ``{"dealname": "ACME renewal", "amount": "50000"}``.

    Returns:
        The created deal.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).create_deal(properties)


@step(retry=_READ)
async def hubspot_get_associations(
    object_type: str, object_id: str, to_object_type: str
) -> list[str]:
    """Ids of the records associated with one record.

    The join a CRM workflow lives on: the deals for a contact, the contacts at
    a company.

    Args:
        object_type: The type of the record you have, e.g. ``"contacts"``.
        object_id: Its id.
        to_object_type: The type you want, e.g. ``"deals"``.

    Returns:
        Associated record ids. Fetch each with ``hubspot_get_object``.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).get_associations(
        object_type, object_id, to_object_type
    )


@step(retry=_READ)
async def hubspot_list_owners(email: str = "", limit: int = 100) -> list[HubSpotOwner]:
    """List HubSpot owners, optionally narrowed by email.

    Resolve a person here before assigning: ``hubspot_owner_id`` takes an owner
    id, and a name passed where an id belongs is silently ignored.

    Args:
        email: Exact email to filter by. Empty returns all owners.
        limit: Maximum owners. Defaults to 100.

    Returns:
        Owners with id, email, and name.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).list_owners(email=email, limit=limit)


@step(retry=_READ)
async def hubspot_account_info() -> HubSpotAccount:
    """Return the HubSpot account this token belongs to.

    The connectivity check the other toolsets spell ``whoami``. Named for what
    it returns: a private app token authenticates an app against a portal, not
    a person, so there is no user to report.

    Returns:
        The account's portal id, type, time zone, currency, and data region.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("hubspot", HubSpotClient)).account_info()
