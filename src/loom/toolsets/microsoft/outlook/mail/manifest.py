"""Outlook mail ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.microsoft.models import MicrosoftUser
from loom.toolsets.microsoft.outlook.models import MailFolder, OutlookMessage


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


OUTLOOK_MAIL_MANIFEST = ToolsetManifest(
    id="outlook_mail",
    version="1.0.0",
    summary="Outlook mail — read, search, send, reply, and organise email.",
    description=(
        "Microsoft Graph v1.0, Exchange Online. Separate from outlook_calendar "
        "so a workflow that reads a calendar need not hold a mail-send scope. "
        "Bodies are returned as TEXT: Graph sends HTML unless asked, so these "
        "tools set Prefer: outlook.body-content-type=text — pass "
        "body_as_html=True for markup. If you combine filter_query and "
        "order_by, every sorted property must also appear in the filter, in "
        "the same order, before any unsorted one, or Graph returns "
        "InefficientFilter. Search ranks by relevance and takes no sort. "
        "Sending returns HTTP 202 = accepted, which is NOT a delivery "
        "confirmation. Well-known folder names ('inbox', 'sentitems', "
        "'drafts', 'deleteditems', 'archive') work anywhere a folder id is "
        "taken. Set MS_OUTLOOK_USER under app-only credentials."
    ),
    base_url="https://graph.microsoft.com/v1.0",
    auth={
        "type": "oauth2",
        "fields": [
            "MS_TENANT_ID",
            "MS_CLIENT_ID",
            "MS_CLIENT_SECRET",
            "MS_REFRESH_TOKEN",
            "MS_GRAPH_ACCESS_TOKEN",
            # Read by the shared auth layer, so declared here: the Azure SDK
            # trio is what a host already has in its environment, and
            # MS_AUTHORITY_HOST is the only way to reach a national cloud.
            # Omitting them told `loom toolset` users to set MS_* variables
            # they did not need.
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "MS_AUTHORITY_HOST",
            "MS_OUTLOOK_USER",
        ],
    },
    tools_module="loom.toolsets.microsoft.outlook.mail.tools",
    egress_hosts=["graph.microsoft.com", "login.microsoftonline.com"],
    rate_limits={
        "model": (
            "dynamic per-workload throttling; honour the Retry-After header "
            "on a 429 rather than assuming a fixed rate"
        ),
        "source": "learn.microsoft.com/en-us/graph/throttling",
    },
    groups={
        "messages": [
            OperationSpec(
                id="messages.list",
                function="outlook_list_messages",
                summary="List messages, newest first.",
                description=(
                    "Bodies are omitted from a listing to keep pages small; "
                    "body_preview is always present. Mind the filter/sort "
                    "ordering contract."
                ),
                effect=EffectClass.READ,
                scopes=["Mail.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(OutlookMessage),
            ),
            OperationSpec(
                id="messages.search",
                function="outlook_search_messages",
                summary="Search by free text, attachments included.",
                description=(
                    "Accepts Outlook field syntax like 'subject:invoice'. "
                    "Ranked by relevance, so there is no sort argument."
                ),
                effect=EffectClass.READ,
                scopes=["Mail.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(OutlookMessage),
            ),
            OperationSpec(
                id="messages.get",
                function="outlook_get_message",
                summary="Fetch one message including its body.",
                effect=EffectClass.READ,
                scopes=["Mail.Read"],
                idempotent=True,
                output_schema=OutlookMessage.model_json_schema(),
            ),
            OperationSpec(
                id="messages.send",
                function="outlook_send_message",
                summary="Send an email.",
                description=(
                    "Returns True meaning ACCEPTED (202), not delivered. Not "
                    "retried: a retry mails everyone twice."
                ),
                effect=EffectClass.WRITE,
                scopes=["Mail.Send"],
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="messages.reply",
                function="outlook_reply_to_message",
                summary="Reply, keeping the conversation together.",
                description="Not retried: a retry sends the reply twice.",
                effect=EffectClass.WRITE,
                scopes=["Mail.Send"],
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="messages.forward",
                function="outlook_forward_message",
                summary="Forward a message.",
                description="Not retried.",
                effect=EffectClass.WRITE,
                scopes=["Mail.Send"],
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="messages.create_draft",
                function="outlook_create_draft",
                summary="Create a draft without sending it.",
                description=(
                    "The safe half of sending: an agent writes, "
                    "ctx.wait_for_approval() parks, a person sends."
                ),
                effect=EffectClass.WRITE,
                scopes=["Mail.ReadWrite"],
                output_schema=OutlookMessage.model_json_schema(),
            ),
            OperationSpec(
                id="messages.send_draft",
                function="outlook_send_draft",
                summary="Send a draft that already exists.",
                description=(
                    "Completes the approval pattern: create_draft writes it, "
                    "ctx.wait_for_approval() parks the run, this sends it. "
                    "Accepted (202) is not delivered. Not retried."
                ),
                effect=EffectClass.WRITE,
                scopes=["Mail.Send"],
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="messages.update",
                function="outlook_update_message",
                summary="Mark read/unread, categorise, or set importance.",
                effect=EffectClass.WRITE,
                scopes=["Mail.ReadWrite"],
                idempotent=True,
                output_schema=OutlookMessage.model_json_schema(),
            ),
            OperationSpec(
                id="messages.move",
                function="outlook_move_message",
                summary="Move a message to another folder.",
                description=(
                    "The message id CHANGES on move — use the returned "
                    "message, not the id you passed in."
                ),
                effect=EffectClass.WRITE,
                scopes=["Mail.ReadWrite"],
                idempotent=True,
                output_schema=OutlookMessage.model_json_schema(),
            ),
            OperationSpec(
                id="messages.delete",
                function="outlook_delete_message",
                summary="Move a message to Deleted Items.",
                description="Recoverable, not a permanent delete.",
                effect=EffectClass.DESTRUCTIVE,
                scopes=["Mail.ReadWrite"],
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
        "folders": [
            OperationSpec(
                id="folders.list",
                function="outlook_list_folders",
                summary="List the mailbox's folders.",
                description=(
                    "Only needed for user-created folders — well-known names "
                    "work as ids directly."
                ),
                resolves="folder",
                effect=EffectClass.READ,
                scopes=["Mail.Read"],
                idempotent=True,
                pagination=True,
                output_schema=_array(MailFolder),
            ),
        ],
        "attachments": [
            OperationSpec(
                id="attachments.list",
                function="outlook_list_attachments",
                summary="List a message's attachments, metadata only.",
                description="Bytes are not inlined; fetch one at a time.",
                effect=EffectClass.READ,
                scopes=["Mail.Read"],
                idempotent=True,
                output_schema={"type": "array", "items": {"type": "object"}},
            ),
            OperationSpec(
                id="attachments.get",
                function="outlook_get_attachment",
                summary="Download one attachment as a LOOM Attachment.",
                description=(
                    "Fails on an item or reference attachment, which carries "
                    "no bytes."
                ),
                effect=EffectClass.READ,
                scopes=["Mail.Read"],
                idempotent=True,
                output_schema={"type": "object", "title": "Attachment"},
            ),
        ],
        "identity": [
            OperationSpec(
                id="identity.whoami",
                function="outlook_whoami",
                summary="The person these credentials authenticate as.",
                resolves="user",
                effect=EffectClass.READ,
                scopes=["User.Read"],
                idempotent=True,
                output_schema=MicrosoftUser.model_json_schema(),
            ),
        ],
    },
)
