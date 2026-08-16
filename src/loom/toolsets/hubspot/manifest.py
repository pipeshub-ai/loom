"""HubSpot ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.hubspot.models import (
    HubSpotAccount,
    HubSpotCompany,
    HubSpotContact,
    HubSpotDeal,
    HubSpotObject,
    HubSpotOwner,
)
from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


HUBSPOT_MANIFEST = ToolsetManifest(
    id="hubspot",
    version="1.0.0",
    summary="HubSpot CRM — contacts, companies, deals, tickets, and owners.",
    description=(
        "HubSpot CRM API v3. List and search any object type including custom "
        "ones, read and write records, follow associations between them, and "
        "resolve a person to the owner id every assignment requires. Search "
        "stops at HubSpot's 10,000-result ceiling and reports the answer as "
        "partial rather than erroring."
    ),
    base_url="https://api.hubapi.com",
    auth={"type": "bearer", "fields": ["HUBSPOT_ACCESS_TOKEN"]},
    tools_module="loom.toolsets.hubspot.tools",
    egress_hosts=["api.hubapi.com"],
    groups={
        "objects": [
            OperationSpec(
                id="objects.list",
                function="hubspot_list_objects",
                summary="List records of any CRM object type.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(HubSpotObject),
            ),
            OperationSpec(
                id="objects.search",
                function="hubspot_search_objects",
                summary="Search one object type by text or property filters.",
                description=(
                    "Capped at 10,000 total results and 5 requests/second. "
                    "Returns partial coverage rather than erroring at the cap."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(HubSpotObject),
            ),
            OperationSpec(
                id="objects.get",
                function="hubspot_get_object",
                summary="Fetch one record by id, or by another unique property.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=HubSpotObject.model_json_schema(),
            ),
            OperationSpec(
                id="objects.create",
                function="hubspot_create_object",
                summary="Create a record of any object type.",
                description="Not retried: HubSpot has no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=HubSpotObject.model_json_schema(),
            ),
            OperationSpec(
                id="objects.update",
                function="hubspot_update_object",
                summary="Update a record. Only the properties passed change.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=HubSpotObject.model_json_schema(),
            ),
            OperationSpec(
                id="objects.archive",
                function="hubspot_archive_object",
                summary="Archive a record.",
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
        "contacts": [
            OperationSpec(
                id="contacts.find",
                function="hubspot_find_contacts",
                summary="Find contacts by name, email, or company.",
                description="The join between a person and the id every write needs.",
                resolves="contact",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(HubSpotContact),
            ),
            OperationSpec(
                id="contacts.get_by_email",
                function="hubspot_get_contact_by_email",
                summary="Fetch one contact by email address.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=HubSpotContact.model_json_schema(),
            ),
            OperationSpec(
                id="contacts.create",
                function="hubspot_create_contact",
                summary="Create a contact.",
                description="Not retried: a retry files a duplicate person.",
                effect=EffectClass.WRITE,
                output_schema=HubSpotContact.model_json_schema(),
            ),
        ],
        "companies": [
            OperationSpec(
                id="companies.find",
                function="hubspot_find_companies",
                summary="Find companies by name or domain.",
                resolves="company",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(HubSpotCompany),
            ),
        ],
        "deals": [
            OperationSpec(
                id="deals.find",
                function="hubspot_find_deals",
                summary="Find deals by free text.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(HubSpotDeal),
            ),
            OperationSpec(
                id="deals.create",
                function="hubspot_create_deal",
                summary="Create a deal.",
                description="Not retried: a retry doubles the forecast.",
                effect=EffectClass.WRITE,
                output_schema=HubSpotDeal.model_json_schema(),
            ),
        ],
        "associations": [
            OperationSpec(
                id="associations.get",
                function="hubspot_get_associations",
                summary="Ids of records associated with one record.",
                description="Deals for a contact, contacts at a company.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema={"type": "array", "items": {"type": "string"}},
            ),
        ],
        "account": [
            OperationSpec(
                id="account.info",
                function="hubspot_account_info",
                summary="The HubSpot account this token belongs to.",
                description=(
                    "The connectivity check other toolsets spell whoami. A "
                    "private app token names a portal, not a person."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=HubSpotAccount.model_json_schema(),
            ),
        ],
        "owners": [
            OperationSpec(
                id="owners.list",
                function="hubspot_list_owners",
                summary="List HubSpot owners, optionally by email.",
                description="Resolve before setting hubspot_owner_id.",
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(HubSpotOwner),
            ),
        ],
    },
)
