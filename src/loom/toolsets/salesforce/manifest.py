"""Salesforce ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
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
    auth=AuthSpec(
        client="loom.toolsets.salesforce.client:SalesforceClient",
        # This client reads environment variables and no CredentialStore, so
        # `credential` is empty and no `provider` is declared: an OAuth flow
        # here would store a token the client never looks up. Adding a store
        # path is a change to the client, not to this manifest.
        # The client owns its own refresh exchange, because every org answers on
        # its own `instance_url` and the login host authenticates without serving
        # data.
        kind="oauth2",
        fields=(
            AuthField(name="SALESFORCE_INSTANCE_URL", arg="instance_url", label="Instance URL",
                      secret=False, example="https://acme.my.salesforce.com"),
            AuthField(name="SALESFORCE_ACCESS_TOKEN", arg="access_token", label="Access token",
                      mode="token"),
            AuthField(name="SALESFORCE_CLIENT_ID",
                      arg="client_id", label="Consumer key", secret=False,
                      mode="refresh"),
            AuthField(name="SALESFORCE_CLIENT_SECRET", arg="client_secret", label="Consumer secret",
                      mode="refresh"),
            AuthField(name="SALESFORCE_REFRESH_TOKEN", arg="refresh_token", label="Refresh token",
                      mode="refresh"),
            AuthField(name="SALESFORCE_LOGIN_URL", arg="login_url", label="Login host (sandbox)",
                      secret=False, required=False,
                      example="https://test.salesforce.com"),
        ),
    ),
    tools_module="loom.toolsets.salesforce.tools",
    egress_hosts=["*.salesforce.com", "*.force.com"],
    rate_limits={
        "model": "org-wide daily API request allocation",
        "signal": (
            "a 403 with errorCode REQUEST_LIMIT_EXCEEDED is org quota and "
            "clears with time; any other 403 is a permission and never will"
        ),
    },
    groups={
        "query": [
            OperationSpec(
                id="query.soql",
                function="salesforce_query",
                summary="Run a SOQL query, paging until Salesforce says done.",
                effect=EffectClass.READ,
                scopes=["api"],
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
                scopes=["api"],
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
                scopes=["api"],
                idempotent=True,
                output_schema=SalesforceRecord.model_json_schema(),
            ),
            OperationSpec(
                id="records.create",
                function="salesforce_create_record",
                summary="Create a record of any object type.",
                description="Not retried: Salesforce has no idempotency key.",
                effect=EffectClass.WRITE,
                scopes=["api"],
                output_schema=SalesforceWriteResult.model_json_schema(),
            ),
            OperationSpec(
                id="records.update",
                function="salesforce_update_record",
                summary="Update a record. Only the fields passed are changed.",
                effect=EffectClass.WRITE,
                scopes=["api"],
                idempotent=True,
                output_schema=SalesforceWriteResult.model_json_schema(),
            ),
            OperationSpec(
                id="records.delete",
                function="salesforce_delete_record",
                summary="Delete a record.",
                effect=EffectClass.DESTRUCTIVE,
                scopes=["api"],
                idempotent=True,
                output_schema=SalesforceWriteResult.model_json_schema(),
            ),
        ],
        "crm": [
            OperationSpec(
                id="crm.find_accounts",
                function="salesforce_find_accounts",
                pagination=True,
                summary="Find accounts by name.",
                description=(
                    "The join between a company's name and the id every write "
                    "requires."
                ),
                resolves="account",
                effect=EffectClass.READ,
                scopes=["api"],
                idempotent=True,
                output_schema=_array(SalesforceAccount),
            ),
            OperationSpec(
                id="crm.find_contacts",
                function="salesforce_find_contacts",
                pagination=True,
                summary="Find contacts by name or email, optionally in one account.",
                resolves="contact",
                effect=EffectClass.READ,
                scopes=["api"],
                idempotent=True,
                output_schema=_array(SalesforceContact),
            ),
            OperationSpec(
                id="crm.find_opportunities",
                function="salesforce_find_opportunities",
                pagination=True,
                summary="Find opportunities by name, account, stage, or openness.",
                effect=EffectClass.READ,
                scopes=["api"],
                idempotent=True,
                output_schema=_array(SalesforceOpportunity),
            ),
        ],
        "people": [
            OperationSpec(
                id="people.find_users",
                function="salesforce_find_users",
                pagination=True,
                summary="Find Salesforce users by name or email.",
                description="Resolve before assigning an OwnerId.",
                resolves="user",
                effect=EffectClass.READ,
                scopes=["api"],
                idempotent=True,
                output_schema=_array(SalesforceUser),
            ),
            OperationSpec(
                id="people.whoami",
                function="salesforce_whoami",
                summary="The user these credentials authenticate as.",
                effect=EffectClass.READ,
                scopes=["api"],
                idempotent=True,
                output_schema=SalesforceUser.model_json_schema(),
            ),
        ],
    },
)
