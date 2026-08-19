"""Workflow: Document Extraction & Summarization.

An invoice, contract, or report arrives as a mail attachment. Pull its text,
read structured fields out of it, summarise it, store both beside the original,
and file the artifact under a name a later run can ask for.

What this shows, and why each part is the way it is:

* **The document parser is a node, not a fake.** Until ``transform.parse_document``
  shipped there was no document-text path in LOOM at all, and the version this
  replaced pretended: it "extracted text" by decoding PDF bytes as UTF-8. The
  node handles PDF, DOCX, and plain text, and reports through ``truncated``
  when a page cap cut a long filing short — a summary of a document that was
  only half read is worse than no summary, because nothing in it says so.

* **Extraction and summarising run in parallel, deliberately bounded.**
  ``ctx.gather`` of two *named* calls, not a comprehension: two is two, and the
  rule that bans unbounded fan-out is about spread that grows with the input.

* **Artifacts, not a storage bucket.** ``ctx.put_artifact`` gives the extracted
  text a name and a version, and republishing identical bytes resolves to the
  existing version rather than inflating the chain — so a retried run does not
  create a second copy.

* **An attachment carries its own identity.** ``gmail_get_attachment`` returns a
  LOOM ``Attachment`` — bytes plus filename, MIME, and size — which is exactly
  what the parser takes. Nothing here passes raw ``bytes`` around and guesses
  the format later.

* **No human gate, and that is a decision.** Everything this touches belongs to
  the organisation already: read a mailbox, write a Drive file and an artifact.
  Nothing leaves and nothing is destroyed.

Credentials: ``GOOGLE_*`` (Gmail + Drive). Reading a PDF needs
``pip install 'loomflow[documents]'``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, Retry, step, workflow
from loom.blobs.attachment import Attachment
from loom.nodes.agentic import ExtractStructuredIn, SummarizeIn
from loom.nodes.documents import ParseDocumentIn
from loom.security.grants import GrantSet
from loom.toolsets.google.drive.tools import drive_upload_file
from loom.toolsets.google.gmail.tools import gmail_get_attachment, gmail_get_message
from loom.triggers.specs import OnAppEvent

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class DocConfig(BaseModel):
    """Which message to read, and where the results go."""

    message_id: str = Field(description="Gmail message id carrying the attachment.")
    max_pages: int = 40
    """0 reads the whole document. The default keeps a long filing from becoming
    a model's context window by accident — and ``truncated`` reports it when it
    fires, rather than leaving a partial read to look complete."""

    drive_folder_id: str = ""
    """Where the summary is filed. Empty means the drive root."""


class ParsedFields(BaseModel):
    """The structured half of a document."""

    title: str = ""
    counterparty: str = ""
    document_date: str = ""
    total_amount: str = ""
    reference: str = ""


class ExtractedDocument(BaseModel):
    """What the workflow returns."""

    message_id: str
    filename: str = ""
    format: str = ""
    page_count: int = 0
    truncated: bool = False
    """Whether the page cap stopped the read before the end of the document.

    Carried all the way out on purpose: a caller acting on ``summary`` without
    checking this is acting on a partial read, and the summary itself will not
    say so."""

    parsed: ParsedFields = Field(default_factory=ParsedFields)
    summary: str = ""
    artifact: str = ""
    drive_file_id: str = ""


#: What ``agent.extract_structured`` is asked to pull out.
DOCUMENT_FIELDS = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "counterparty": {"type": "string", "description": "The other party named"},
        "document_date": {"type": "string", "description": "The document's own date"},
        "total_amount": {"type": "string", "description": "Total, with its currency"},
        "reference": {"type": "string", "description": "Invoice or contract number"},
    },
}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def first_attachment(message_id: str) -> Attachment | None:
    """The message's first attachment, bytes and all, or ``None``.

    Two calls, because a message carries attachment *metadata* until asked:
    ``gmail_get_message`` reports ids and sizes, and this fetches one. Returning
    ``None`` rather than raising is what lets the workflow report "no
    attachment" as a fact instead of a failure — a mail with nothing attached
    is an ordinary thing for a mailbox rule to match.
    """
    message = await gmail_get_message(message_id=message_id)
    if not message.attachments:
        return None
    ref = message.attachments[0]
    return await gmail_get_attachment(
        message_id=message_id,
        attachment_id=ref.attachment_id,
        filename=ref.filename,
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def file_summary(name: str, text: str, folder_id: str) -> str:
    """Put the summary in Drive.

    Retried: re-uploading produces a second file with the same name, which is
    recoverable and visible. That is a different bargain from a mail send, and
    it is why the two are not treated alike.
    """
    uploaded = await drive_upload_file(
        name=name,
        content=text,
        mime_type="text/plain",
        folder_id=folder_id,
    )
    return uploaded.id


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="doc_extraction",
    version="2",
    triggers=[OnAppEvent("app.gmail.message")],
    grants=GrantSet(toolsets=["gmail", "google_drive"]),
)
async def doc_extraction(ctx: Context, config: DocConfig) -> ExtractedDocument:
    """Read an attachment, parse it, extract and summarise, then file both."""
    attachment = await ctx.step(first_attachment, config.message_id)
    if attachment is None:
        return ExtractedDocument(message_id=config.message_id)

    document = await ctx.node(
        "transform.parse_document",
        ParseDocumentIn(document=attachment, max_pages=config.max_pages),
    )

    # Two named calls, not a comprehension. Reading fields and writing a summary
    # are independent, so they overlap; the count is fixed at two, so nothing
    # here spreads with the size of the input.
    fields, summary = await ctx.gather(
        ctx.node(
            "agent.extract_structured",
            ExtractStructuredIn(text=document.text[:20_000], fields=DOCUMENT_FIELDS),
            name="extract_fields",
        ),
        ctx.node(
            "agent.summarize",
            SummarizeIn(
                text=document.text[:20_000],
                style="a short paragraph",
                focus="what this document commits either party to",
            ),
            name="write_summary",
        ),
    )

    parsed = ParsedFields(
        **{
            key: str(value)
            for key, value in fields.values.items()
            if key in ParsedFields.model_fields
        }
    )

    # Immutable and versioned, and identical bytes resolve to the existing
    # version — so a retried run does not add a second copy.
    artifact = await ctx.put_artifact(
        f"document:{config.message_id}",
        document.text.encode("utf-8"),
        mime="text/plain",
    )

    drive_id = await ctx.step(
        file_summary,
        f"{attachment.filename} — summary.txt",
        summary.summary,
        config.drive_folder_id,
    )

    return ExtractedDocument(
        message_id=config.message_id,
        filename=attachment.filename,
        format=document.format,
        page_count=document.page_count,
        truncated=document.truncated,
        parsed=parsed,
        summary=summary.summary,
        artifact=str(artifact),
        drive_file_id=drive_id,
    )
