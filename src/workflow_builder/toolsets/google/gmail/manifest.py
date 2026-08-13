"""Gmail toolset manifest.

Output schemas come from the Pydantic models, so the contract the coding agent
reads and the contract the client honours cannot drift apart.
"""

from __future__ import annotations

from workflow_builder.toolsets.google.gmail.models import (
    EmailMessage,
    GmailLabel,
    GmailProfile,
    SentMessage,
)
from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = ["GMAIL_MANIFEST"]

_message = EmailMessage.model_json_schema()
_message_list = {"type": "array", "items": _message}
_message_id = {
    "type": "object",
    "properties": {"message_id": {"type": "string"}},
    "required": ["message_id"],
}

GMAIL_MANIFEST = ToolsetManifest(
    id="gmail",
    version="1.0.0",
    provider="loom",
    summary=(
        "Gmail — search the inbox, read and send email, reply, label, archive."
    ),
    description=(
        "Gmail API v1 over REST. Search mail with Gmail query syntax, read "
        "inbox messages flattened out of MIME, send email and reply in-thread, "
        "star, archive and label messages, and "
        "download attachments as LOOM Attachments. Sending is deliberately not "
        "retried automatically: Gmail has no idempotency key, so a retry after "
        "a post-delivery timeout would send twice."
    ),
    base_url="https://gmail.googleapis.com/gmail/v1",
    auth={
        "type": "oauth2",
        "fields": [
            "GOOGLE_ACCESS_TOKEN",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
        ],
        "token_url": "https://oauth2.googleapis.com/token",
    },
    tools_module="workflow_builder.toolsets.google.gmail.tools",
    egress_hosts=["gmail.googleapis.com", "oauth2.googleapis.com"],
    rate_limits={"quota_units_per_user_per_second": 250},
    groups={
        "messages": [
            OperationSpec(
                id="messages.search",
                function="gmail_search_messages",
                summary="Search the mailbox with Gmail query syntax.",
                description=(
                    "Costs one request per hit on top of the search itself, "
                    "because the list endpoint returns bare ids."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 20},
                    },
                },
                output_schema=_message_list,
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="messages.get",
                function="gmail_get_message",
                summary="Fetch one message with its body and attachment list.",
                effect=EffectClass.READ,
                input_schema=_message_id,
                output_schema=_message,
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="messages.send",
                function="gmail_send_message",
                summary="Send an email.",
                description=(
                    "Not idempotent and not automatically retried — Gmail "
                    "offers no idempotency key."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "cc": {"type": "array", "items": {"type": "string"}},
                        "bcc": {"type": "array", "items": {"type": "string"}},
                        "html": {"type": "boolean", "default": False},
                    },
                    "required": ["to", "subject", "body"],
                },
                output_schema=SentMessage.model_json_schema(),
                scopes=["https://www.googleapis.com/auth/gmail.send"],
            ),
            OperationSpec(
                id="messages.reply",
                function="gmail_reply_to_message",
                summary="Reply to a message in its own thread.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "body": {"type": "string"},
                        "reply_all": {"type": "boolean", "default": False},
                    },
                    "required": ["message_id", "body"],
                },
                output_schema=SentMessage.model_json_schema(),
                scopes=["https://www.googleapis.com/auth/gmail.send"],
            ),
            OperationSpec(
                id="messages.modify_labels",
                function="gmail_modify_labels",
                summary="Add or remove labels on a message.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "add": {"type": "array", "items": {"type": "string"}},
                        "remove": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["message_id"],
                },
                output_schema=_message,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                idempotent=True,
            ),
            OperationSpec(
                id="messages.trash",
                function="gmail_trash_message",
                summary="Move a message to the trash.",
                description="Recoverable for 30 days. Permanent delete is not exposed.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema=_message_id,
                output_schema=_message,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
            ),
            OperationSpec(
                id="messages.get_attachment",
                function="gmail_get_attachment",
                summary="Download an attachment as a LOOM Attachment.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "attachment_id": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    "required": ["message_id", "attachment_id"],
                },
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
            ),
        ],
        "labels": [
            OperationSpec(
                id="labels.list",
                function="gmail_list_labels",
                summary="List all labels, system and user-created.",
                effect=EffectClass.READ,
                output_schema={
                    "type": "array",
                    "items": GmailLabel.model_json_schema(),
                },
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
            ),
        ],
        "profile": [
            OperationSpec(
                id="profile.get",
                function="gmail_get_profile",
                summary="Get the authenticated mailbox's profile.",
                effect=EffectClass.READ,
                output_schema=GmailProfile.model_json_schema(),
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
            ),
        ],
    },
)
