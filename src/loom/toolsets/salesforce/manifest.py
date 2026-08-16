"""Salesforce ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.salesforce.models import (
    SalesforceAccount,
    SalesforceContact,
    SalesforceOpportunity,
    SalesforceRecord,
    SalesforceUser,
    SalesforceWriteResult,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


SALESFORCE_MANIFEST = ToolsetManifest(
    id="salesforce",
    version="1.0.0",
    summary="Salesforce CRM — SOQL, sObject CRUD, and account/contact/deal finders.",
    description=(
        "Salesforce REST API. Run SOQL with automatic paging, read and write "
        "any object including custom ones, and resolve company, person, and "
        "user names to the ids every write requires. The base URL is per-org "
        "(instance_url) and access tokens are refreshed by the client."
    ),
    base_url="https://<instance>.my.salesforce.com/services/data",
    auth={
        "type": "oauth2",
        "fields": [
            "SALESFORCE_INSTANCE_URL",
            "SALESFORCE_ACCESS_TOKEN",
            "SALESFORCE_CLIENT_ID",
            "SALESFORCE_CLIENT_SECRET",
            "SALESFORCE_REFRESH_TOKEN",
            "SALESFORCE_LOGIN_URL",
        ],
    },
    tools_module="loom.toolsets.salesforce.tools",
    egress_hosts=["*.salesforce.com", "*.force.com"],
    groups={
        "query": [
            OperationSpec(
                id="query.soql",
                function="salesforce_query",
                summary="Run a SOQL query, paging until Salesforce says done.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(SalesforceRecord),
            ),
            OperationSpec(
                id="query.describe",
                function="salesforce_describe_object",
                summary="Describe an object's fields, types, and writability.",
                description=(
                    "Call before writing SOQL against an org whose custom "
                    "fields cannot be guessed."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema={"type": "object"},
            ),
        ],
        "records": [
            OperationSpec(
                id="records.get",
                function="salesforce_get_record",
                summary="Fetch one record of any object type.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=SalesforceRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.create",
                function="salesforce_create_record",
                summary="Create a record of any object type.",
                description="Not retried: Salesforce has no idempotency key.",
                effect=EffectClass.WRITE,
                output_schema=SalesforceWriteResult.model_json_schema(),
            ),
            OperationSpec(
                id="records.update",
                function="salesforce_update_record",
                summary="Update a record. Only the fields passed are changed.",
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=SalesforceWriteResult.model_json_schema(),
            ),
            OperationSpec(
                id="records.delete",
                function="salesforce_delete_record",
                summary="Delete a record.",
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema=SalesforceWriteResult.model_json_schema(),
            ),
        ],
        "crm": [
            OperationSpec(
                id="crm.find_accounts",
                function="salesforce_find_accounts",
                summary="Find accounts by name.",
                description=(
                    "The join between a company's name and the id every write "
                    "requires."
                ),
                resolves="account",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(SalesforceAccount),
            ),
            OperationSpec(
                id="crm.find_contacts",
                function="salesforce_find_contacts",
                summary="Find contacts by name or email, optionally in one account.",
                resolves="contact",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(SalesforceContact),
            ),
            OperationSpec(
                id="crm.find_opportunities",
                function="salesforce_find_opportunities",
                summary="Find opportunities by name, account, stage, or openness.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(SalesforceOpportunity),
            ),
        ],
        "people": [
            OperationSpec(
                id="people.find_users",
                function="salesforce_find_users",
                summary="Find Salesforce users by name or email.",
                description="Resolve before assigning an OwnerId.",
                resolves="user",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=_array(SalesforceUser),
            ),
            OperationSpec(
                id="people.whoami",
                function="salesforce_whoami",
                summary="The user these credentials authenticate as.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=SalesforceUser.model_json_schema(),
            ),
        ],
    },
)
