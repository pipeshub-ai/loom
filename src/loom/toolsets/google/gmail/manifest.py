"""Gmail toolset manifest.

Output schemas come from the Pydantic models, so the contract the coding agent
reads and the contract the client honours cannot drift apart.
"""

from __future__ import annotations

from loom.toolsets.google.gmail.models import (
    EmailMessage,
    EmailThread,
    GmailDraft,
    GmailLabel,
    GmailProfile,
    SentMessage,
)
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

__all__ = ["GMAIL_MANIFEST"]

_message = EmailMessage.model_json_schema()
_message_list = {"type": "array", "items": _message}
_thread = EmailThread.model_json_schema()
_draft = GmailDraft.model_json_schema()
_label = GmailLabel.model_json_schema()
_message_id = {
    "type": "object",
    "properties": {"message_id": {"type": "string"}},
    "required": ["message_id"],
}
_thread_id = {
    "type": "object",
    "properties": {"thread_id": {"type": "string"}},
    "required": ["thread_id"],
}
_labels_patch = {
    "add": {"type": "array", "items": {"type": "string"}},
    "remove": {"type": "array", "items": {"type": "string"}},
}
_compose = {
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
}

GMAIL_MANIFEST = ToolsetManifest(
    id="gmail",
    version="1.0.0",
    provider="loom",
    summary=(
        "Gmail — search the inbox, read and send email, reply, forward, "
        "draft, label, archive, and work with whole conversations."
    ),
    description=(
        "Gmail API v1 over REST. Search mail with Gmail query syntax, read "
        "inbox messages flattened out of MIME, read and label whole "
        "conversations, send email with attachments, reply in-thread and "
        "forward, compose drafts for a human to approve and send, star, "
        "archive and label messages singly or a thousand at a time, manage "
        "user labels, and download attachments as LOOM Attachments. Sending is "
        "deliberately not retried automatically: Gmail has no idempotency key, "
        "so a retry after a post-delivery timeout would send twice."
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
    tools_module="loom.toolsets.google.gmail.tools",
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
                    "properties": {**_compose, "attachments": {"type": "array"}},
                    "required": ["to", "subject", "body"],
                },
                output_schema=SentMessage.model_json_schema(),
                scopes=["https://www.googleapis.com/auth/gmail.send"],
            ),
            OperationSpec(
                id="messages.forward",
                function="gmail_forward_message",
                summary="Forward a message, quoting the original.",
                description=(
                    "Sends a new message rather than adding a recipient to the "
                    "thread, which would deliver the whole prior conversation "
                    "to someone who was never in it. Not retried."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "to": _compose["to"],
                        "comment": {"type": "string"},
                        "html": {"type": "boolean", "default": False},
                    },
                    "required": ["message_id", "to"],
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
                id="messages.batch_modify_labels",
                function="gmail_batch_modify_labels",
                summary="Add or remove labels on up to 1000 messages at once.",
                description=(
                    "Gmail charges quota per request, so this is one unit "
                    "where a loop would be hundreds."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 1000,
                        },
                        **_labels_patch,
                    },
                    "required": ["message_ids"],
                },
                output_schema={"type": "integer"},
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                idempotent=True,
            ),
            OperationSpec(
                id="messages.trash",
                function="gmail_trash_message",
                summary="Move a message to the trash.",
                description=(
                    "Recoverable for 30 days. Permanent delete is not "
                    "exposed: it needs Google's restricted full-mailbox "
                    "scope, which no other operation here requires."
                ),
                effect=EffectClass.DESTRUCTIVE,
                input_schema=_message_id,
                output_schema=_message,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                idempotent=True,
            ),
            OperationSpec(
                id="messages.untrash",
                function="gmail_untrash_message",
                summary="Take a message back out of the trash.",
                effect=EffectClass.WRITE,
                input_schema=_message_id,
                output_schema=_message,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                idempotent=True,
            ),
        ],
        "threads": [
            OperationSpec(
                id="threads.list",
                function="gmail_list_threads",
                summary="Search conversations — ids and snippets only.",
                description=(
                    "One request per page rather than one per hit, so this is "
                    "the cheap way to triage an inbox. Fetch the threads that "
                    "matter with threads.get."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 20},
                        "label_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                output_schema={"type": "array", "items": _thread},
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="threads.get",
                function="gmail_get_thread",
                summary="Fetch a whole conversation, every message flattened.",
                effect=EffectClass.READ,
                input_schema=_thread_id,
                output_schema=_thread,
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="threads.modify_labels",
                function="gmail_modify_thread_labels",
                summary="Add or remove labels on an entire conversation.",
                description=(
                    "Usually the right unit for triage: Gmail groups by "
                    "thread, so labelling one message looks like nothing "
                    "happened."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string"},
                        **_labels_patch,
                    },
                    "required": ["thread_id"],
                },
                output_schema=_thread,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                idempotent=True,
            ),
            OperationSpec(
                id="threads.trash",
                function="gmail_trash_thread",
                summary="Move a whole conversation to the trash.",
                description="Recoverable for 30 days.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema=_thread_id,
                output_schema=_thread,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                idempotent=True,
            ),
        ],
        "drafts": [
            OperationSpec(
                id="drafts.create",
                function="gmail_create_draft",
                summary="Compose an email without sending it.",
                description=(
                    "The safe half of sending, and the basis of a "
                    "draft-then-approve workflow. Retried, unlike sending, "
                    "because a duplicate draft reaches nobody."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        **_compose,
                        "thread_id": {"type": "string"},
                        "attachments": {"type": "array"},
                    },
                    "required": ["to", "subject", "body"],
                },
                output_schema=_draft,
                scopes=["https://www.googleapis.com/auth/gmail.compose"],
                idempotent=True,
            ),
            OperationSpec(
                id="drafts.list",
                function="gmail_list_drafts",
                summary="List unsent drafts.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer", "default": 20}
                    },
                },
                output_schema={"type": "array", "items": _draft},
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="drafts.send",
                function="gmail_send_draft",
                summary="Send an existing draft.",
                description="Not retried — a retry could send it twice.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {"draft_id": {"type": "string"}},
                    "required": ["draft_id"],
                },
                output_schema=SentMessage.model_json_schema(),
                scopes=["https://www.googleapis.com/auth/gmail.send"],
            ),
            OperationSpec(
                id="drafts.delete",
                function="gmail_delete_draft",
                summary="Discard a draft. It was never delivered.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {"draft_id": {"type": "string"}},
                    "required": ["draft_id"],
                },
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/gmail.compose"],
            ),
        ],
        "labels": [
            OperationSpec(
                id="labels.list",
                function="gmail_list_labels",
                summary="List all labels, system and user-created.",
                effect=EffectClass.READ,
                output_schema={"type": "array", "items": _label},
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
            ),
            OperationSpec(
                id="labels.find",
                function="gmail_find_label",
                summary="Resolve a label name to its label id.",
                description=(
                    "Labelling takes ids like 'Label_7'. Passing the name a "
                    "person used is not an error — Gmail applies nothing and "
                    "reports success."
                ),
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {"label_name": {"type": "string"}},
                    "required": ["label_name"],
                },
                output_schema=_label,
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                idempotent=True,
                resolves="label",
            ),
            OperationSpec(
                id="labels.create",
                function="gmail_create_label",
                summary="Create a user label.",
                description=(
                    "A '/' nests it — 'Clients/Acme' puts Acme under Clients, "
                    "and the parent must already exist."
                ),
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                output_schema=_label,
                scopes=["https://www.googleapis.com/auth/gmail.labels"],
            ),
            OperationSpec(
                id="labels.rename",
                function="gmail_rename_label",
                summary="Rename a user label. System labels cannot be renamed.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "label_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["label_id", "name"],
                },
                output_schema=_label,
                scopes=["https://www.googleapis.com/auth/gmail.labels"],
                idempotent=True,
            ),
            OperationSpec(
                id="labels.delete",
                function="gmail_delete_label",
                summary="Delete a user label. The messages survive.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {"label_id": {"type": "string"}},
                    "required": ["label_id"],
                },
                output_schema={"type": "string"},
                scopes=["https://www.googleapis.com/auth/gmail.labels"],
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
