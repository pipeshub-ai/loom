"""``transform.parse_document`` — bytes of a document, out as text.

Two of the reference workflows need this and neither could have it: LOOM had no
document-text path at all, so both faked one. A PDF arrives from a mailbox, a
Drive export, or an upload, and *everything* downstream — summarize, extract
fields, chunk and index — needs it as text first.

In ``transform`` rather than ``io`` because it reaches nothing: given the same
bytes it returns the same text, so it is free to recompute on replay and needs
no journal entry of its own.

Two properties worth the space they take:

**A page cap reports itself.** ``max_pages`` exists because a thousand-page
filing should not become a model's context window by accident, but a cap that
truncates silently makes the next step summarize a document it only partly read
— the same failure ``Results.complete`` exists to prevent one layer up. So
``truncated`` is on the output, and the page count is the document's, not the
number returned.

**An unsupported format is refused by name.** Guessing at bytes and returning
whatever decodes produces mojibake that reads as a broken document rather than
as the wrong parser. The error names the format it found and the formats it
handles.

``pypdf`` and ``python-docx`` are an optional extra::

    pip install 'loomflow[documents]'

Plain text, Markdown, CSV, and JSON need nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any

from pydantic import BaseModel, Field

from loom.blobs.attachment import Attachment
from loom.core.exceptions import ConfigurationError
from loom.nodes.base import Node, NodeContext
from loom.nodes.registry import register_node
from loom.nodes.spec import NodeCategory, NodeExample, NodeSpec

__all__ = ["Page", "ParseDocumentIn", "ParseDocumentNode", "ParseDocumentOut"]

#: Extension to the format name this node reports. The mime type is consulted
#: first; a filename is the fallback, because a Drive export and a mail
#: attachment frequently arrive as ``application/octet-stream``.
_BY_EXTENSION = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "text",
    "text": "text",
    "md": "text",
    "markdown": "text",
    "csv": "text",
    "log": "text",
    "json": "json",
}

_BY_MIME = {
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "text",
    "text/markdown": "text",
    "text/csv": "text",
    "application/json": "json",
}

#: What a PDF starts with. Checked when neither the mime type nor the filename
#: says anything, since ``application/octet-stream`` is what most mail servers
#: label an attachment.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"


class Page(BaseModel):
    """One page's text, numbered from 1 as a reader would count them."""

    number: int
    text: str = ""


class ParseDocumentIn(BaseModel):
    document: Attachment = Field(
        description="The file to read. Its mime type or filename names the format."
    )
    max_pages: int = Field(
        default=0,
        description=(
            "Stop after this many pages. 0 reads all of them. When a cap cuts "
            "the document short, `truncated` says so and `page_count` still "
            "reports the whole document."
        ),
    )
    join_with: str = Field(
        default="\n\n",
        description="What to put between pages when joining them into `text`.",
    )


class ParseDocumentOut(BaseModel):
    text: str = ""
    """Every page returned, joined. The field most callers want."""

    pages: list[Page] = Field(default_factory=list)
    """Per page, so a citation can name where an answer came from."""

    page_count: int = 0
    """Pages in the *document*, not pages returned. See `truncated`."""

    truncated: bool = False
    """Whether `max_pages` stopped the read before the end.

    A caller that summarises without checking this reports on a document it
    only partly read, and nothing in the text says so."""

    format: str = ""
    """`pdf`, `docx`, `text`, or `json`."""

    filename: str = ""


@register_node
class ParseDocumentNode(Node[ParseDocumentIn, ParseDocumentOut]):
    """Extract text from a PDF, Word document, or plain-text file."""

    spec = NodeSpec(
        id="transform.parse_document",
        import_module="loom.nodes.documents",
        category=NodeCategory.TRANSFORM,
        deterministic=True,
        summary="Extract text from a PDF, Word document, or plain-text file.",
        description=(
            "Takes an Attachment and returns its text, per page and joined. "
            "PDF and DOCX need the 'documents' extra; text, Markdown, CSV and "
            "JSON need nothing. A page cap reports itself through `truncated` "
            "rather than silently shortening the document."
        ),
        tags=["pdf", "docx", "document", "text", "extract", "ocr", "parse"],
        examples=[
            NodeExample(
                payload={
                    "document": {
                        "filename": "invoice.pdf",
                        "mime": "application/pdf",
                    },
                    "max_pages": 10,
                }
            )
        ],
    )
    Input, Output = ParseDocumentIn, ParseDocumentOut

    async def run(self, ctx: NodeContext, payload: ParseDocumentIn) -> ParseDocumentOut:
        data = await payload.document.read(getattr(ctx, "blobs", None))
        fmt = detect_format(payload.document, data)

        if fmt == "pdf":
            pages, total = _read_pdf(data, payload.max_pages)
        elif fmt == "docx":
            pages, total = _read_docx(data)
        else:
            pages, total = _read_text(data, fmt)

        return ParseDocumentOut(
            text=payload.join_with.join(page.text for page in pages),
            pages=pages,
            page_count=total,
            truncated=len(pages) < total,
            format=fmt,
            filename=payload.document.filename,
        )


def detect_format(document: Attachment, data: bytes) -> str:
    """Name the format, or raise saying what was found and what is handled.

    Mime type first, then the filename, then the leading bytes — in that order
    because the first is a declaration, the second a convention, and the third a
    guess. A mail attachment routinely arrives as ``application/octet-stream``
    with a truthful name, and a Drive export routinely arrives with neither, so
    all three are needed.
    """
    mime = (document.mime or "").split(";")[0].strip().lower()
    if mime in _BY_MIME:
        return _BY_MIME[mime]

    extension = document.filename.rsplit(".", 1)[-1].lower() if "." in document.filename else ""
    if extension in _BY_EXTENSION:
        return _BY_EXTENSION[extension]

    if data.startswith(_PDF_MAGIC):
        return "pdf"
    if data.startswith(_ZIP_MAGIC) and extension in {"", "zip"}:
        # A .docx is a zip. Saying so beats decoding it as text and returning
        # the archive's binary header as a document's contents.
        raise ConfigurationError(
            f"{document.filename or 'the document'} looks like a zip archive. "
            "If it is a Word document, name it .docx or set its mime type."
        )
    if mime.startswith("text/"):
        return "text"

    raise ConfigurationError(
        f"cannot parse {document.filename or 'the document'}: unrecognised format"
        f"{f' (mime {mime})' if mime else ''}. "
        "transform.parse_document handles pdf, docx, text, markdown, csv, and json."
    )


def _require(package: str, import_name: str) -> Any:
    """Import an optional parser, or say which extra supplies it."""
    try:
        return __import__(import_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"reading this document needs {package}, which is not installed. "
            f"pip install 'loomflow[documents]'"
        ) from exc


def _read_pdf(data: bytes, max_pages: int) -> tuple[list[Page], int]:
    _require("pypdf", "pypdf")
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    wanted = total if max_pages <= 0 else min(max_pages, total)
    pages = [
        Page(number=index + 1, text=reader.pages[index].extract_text() or "")
        for index in range(wanted)
    ]
    return pages, total


def _read_docx(data: bytes) -> tuple[list[Page], int]:
    """Word paragraphs, as one page.

    A .docx has no pages — pagination is decided when it is rendered — so
    reporting one is the honest answer rather than inventing page breaks. That
    also means ``max_pages`` cannot truncate a Word document, and does not
    pretend to.
    """
    _require("python-docx", "docx")
    import docx

    document = docx.Document(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    return [Page(number=1, text=text)], 1


def _read_text(data: bytes, fmt: str) -> tuple[list[Page], int]:
    text = data.decode("utf-8", errors="replace")
    if fmt == "json":
        # Re-rendered rather than passed through, so a minified payload is
        # readable by whatever reads the text next. Invalid JSON falls through
        # as its own bytes: those are still what somebody wants to look at, and
        # raising would turn a readable file into an error.
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), indent=2)
    return [Page(number=1, text=text)], 1
