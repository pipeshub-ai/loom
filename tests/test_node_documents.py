"""``transform.parse_document`` — the text path LOOM did not have.

Two reference workflows need a PDF as text and both faked it, because there was
no document parser anywhere in the library. These tests build real files rather
than asserting against fixtures, so a parser upgrade that changes extraction is
visible here rather than in somebody's workflow.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from loom.blobs.attachment import Attachment
from loom.core.exceptions import ConfigurationError
from loom.nodes.documents import (
    ParseDocumentIn,
    ParseDocumentNode,
    ParseDocumentOut,
    detect_format,
)

pypdf = pytest.importorskip("pypdf", reason="needs the [documents] extra")


def a_pdf(page_count: int) -> bytes:
    """A real PDF of *page_count* blank pages.

    Blank on purpose: laying down text needs a font resource and a content
    stream, which is pypdf's business to change between versions and not what
    these cases are about. What they assert is *page counting* and the
    truncation report, and a blank page counts exactly like a full one.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def parse(document: Attachment, **kwargs: Any) -> ParseDocumentOut:
    """Run the node directly. Its body reaches nothing, so it needs no context."""
    node = ParseDocumentNode()
    return await node.run(None, ParseDocumentIn(document=document, **kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Format detection — three sources, in order of how much they can be trusted
# ---------------------------------------------------------------------------


class TestDetectFormat:
    def test_mime_type_wins(self) -> None:
        att = Attachment(filename="report.bin", mime="application/pdf")
        assert detect_format(att, b"") == "pdf"

    def test_filename_when_the_mime_type_says_nothing(self) -> None:
        """A mail attachment routinely arrives as application/octet-stream."""
        att = Attachment(filename="report.pdf", mime="application/octet-stream")
        assert detect_format(att, b"") == "pdf"

    def test_leading_bytes_when_neither_does(self) -> None:
        att = Attachment(filename="", mime="")
        assert detect_format(att, b"%PDF-1.7\nrest") == "pdf"

    def test_a_zip_is_named_rather_than_decoded(self) -> None:
        """A .docx is a zip; decoding one as text returns its binary header."""
        att = Attachment(filename="export", mime="")
        with pytest.raises(ConfigurationError, match="zip archive"):
            detect_format(att, b"PK\x03\x04rest")

    def test_an_unknown_format_names_what_is_supported(self) -> None:
        att = Attachment(filename="scan.tiff", mime="image/tiff")
        with pytest.raises(ConfigurationError, match="pdf, docx, text"):
            detect_format(att, b"II*\x00")

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("notes.md", "text"),
            ("rows.csv", "text"),
            ("app.log", "text"),
            ("payload.json", "json"),
            ("memo.docx", "docx"),
        ],
    )
    def test_extensions(self, filename: str, expected: str) -> None:
        assert detect_format(Attachment(filename=filename, mime=""), b"") == expected


# ---------------------------------------------------------------------------
# Text and JSON — no extra needed
# ---------------------------------------------------------------------------


class TestPlainText:
    @pytest.mark.asyncio()
    async def test_text_round_trips(self) -> None:
        att = Attachment.from_text("notes.txt", "hello\nworld")
        out = await parse(att)
        assert out.text == "hello\nworld"
        assert out.format == "text"
        assert out.page_count == 1
        assert out.truncated is False
        assert out.filename == "notes.txt"

    @pytest.mark.asyncio()
    async def test_json_is_re_rendered_readably(self) -> None:
        """A minified payload is unreadable to whatever reads the text next."""
        att = Attachment.from_bytes(
            "payload.json", json.dumps({"b": 2, "a": 1}).encode(), mime="application/json"
        )
        out = await parse(att)
        assert "\n" in out.text
        assert json.loads(out.text) == {"b": 2, "a": 1}

    @pytest.mark.asyncio()
    async def test_invalid_json_is_returned_as_text(self) -> None:
        """Better than raising: the bytes are still what somebody wants to see."""
        att = Attachment.from_bytes("broken.json", b"{not json", mime="application/json")
        out = await parse(att)
        assert out.text == "{not json"


# ---------------------------------------------------------------------------
# PDF — page counting and the cap that has to report itself
# ---------------------------------------------------------------------------


class TestPdf:
    @pytest.mark.asyncio()
    async def test_every_page_is_returned_and_counted(self) -> None:
        att = Attachment.from_bytes(
            "doc.pdf", a_pdf(3), mime="application/pdf"
        )
        out = await parse(att)
        assert out.format == "pdf"
        assert out.page_count == 3
        assert [p.number for p in out.pages] == [1, 2, 3]
        assert out.truncated is False

    @pytest.mark.asyncio()
    async def test_a_page_cap_reports_itself(self) -> None:
        """The property this node exists to get right.

        A cap that shortens a document without saying so makes the next step
        summarise a filing it only partly read — the same failure ``Results``
        prevents one layer up.
        """
        att = Attachment.from_bytes(
            "doc.pdf", a_pdf(5), mime="application/pdf"
        )
        out = await parse(att, max_pages=2)
        assert len(out.pages) == 2
        assert out.truncated is True
        assert out.page_count == 5, "the count is the document's, not the read's"

    @pytest.mark.asyncio()
    async def test_a_cap_above_the_length_does_not_claim_truncation(self) -> None:
        att = Attachment.from_bytes("doc.pdf", a_pdf(1), mime="application/pdf")
        out = await parse(att, max_pages=99)
        assert out.truncated is False


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


class TestDocx:
    @pytest.mark.asyncio()
    async def test_paragraphs_come_back_as_one_page(self) -> None:
        """A .docx has no pages until it is rendered, so reporting one is honest."""
        docx = pytest.importorskip("docx", reason="needs the [documents] extra")

        document = docx.Document()
        document.add_paragraph("first")
        document.add_paragraph("second")
        buffer = io.BytesIO()
        document.save(buffer)

        att = Attachment.from_bytes("memo.docx", buffer.getvalue())
        out = await parse(att)
        assert out.format == "docx"
        assert out.text == "first\nsecond"
        assert out.page_count == 1
        assert out.truncated is False


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestRegistered:
    def test_the_node_is_in_the_catalog(self) -> None:
        """A node the coding agent cannot find is a node nobody will use."""
        from loom.nodes import get_node_catalog, load_builtin_nodes

        load_builtin_nodes()
        assert "transform.parse_document" in get_node_catalog().node_ids()

    def test_it_declares_itself_deterministic(self) -> None:
        """Same bytes, same text — so it is free to recompute on replay."""
        assert ParseDocumentNode.spec.deterministic is True
