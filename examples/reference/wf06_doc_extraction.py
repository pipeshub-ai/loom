"""Workflow: Document Extraction & Summarization."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from workflow_builder import Context, Retry, step, workflow

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DocConfig(BaseModel):
    """Input configuration for document extraction."""

    document_url: str
    document_id: str
    storage_bucket: str = "processed-docs"
    openai_api_key: str = ""


class ParsedFields(BaseModel):
    """Structured fields extracted by AI."""

    title: str = ""
    author: str = ""
    date: str = ""
    entities: list[str] = []
    key_terms: list[str] = []


class DocSummary(BaseModel):
    """AI-generated summary of a document."""

    executive_summary: str = ""
    key_points: list[str] = []
    word_count: int = 0


class ExtractedDocument(BaseModel):
    """Final output combining parsed fields and summary."""

    document_id: str
    raw_text: str
    parsed: ParsedFields
    summary: DocSummary
    storage_path: str


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, delay=2.0))
async def download_attachment(
    url: str,
) -> bytes:
    """Download a document from a URL.

    Args:
        url: The URL to download from.

    Returns:
        Raw file bytes.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


@step
async def extract_text(raw_bytes: bytes) -> str:
    """Extract plain text from raw document bytes.

    Args:
        raw_bytes: The raw file content.

    Returns:
        Extracted text content.
    """
    # In production, use a library like PyPDF2, pdfplumber,
    # or python-docx depending on the file type.
    return raw_bytes.decode("utf-8", errors="replace")


@step(retry=Retry(max_attempts=2, delay=1.0))
async def ai_parse_document(
    text: str,
    api_key: str,
) -> ParsedFields:
    """Use an LLM to extract structured fields from text.

    Args:
        text: The plain text to parse.
        api_key: OpenAI API key.

    Returns:
        Extracted structured fields.
    """
    prompt = (
        "Extract the following from this document: "
        "title, author, date, entities, key_terms.\n\n"
        f"{text[:3000]}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    # Simulated parse of LLM response into structured fields
    return ParsedFields(
        title=content[:50] if content else "Untitled",
        entities=["entity_a", "entity_b"],
        key_terms=["term_1", "term_2"],
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def summarize_document(
    text: str,
    api_key: str,
) -> DocSummary:
    """Use an LLM to generate a summary of the document.

    Args:
        text: The plain text to summarize.
        api_key: OpenAI API key.

    Returns:
        AI-generated summary.
    """
    prompt = (
        "Summarize this document in 2-3 sentences, "
        "then list 3-5 key points.\n\n"
        f"{text[:4000]}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    return DocSummary(
        executive_summary=content[:200] if content else "",
        key_points=["Point 1", "Point 2", "Point 3"],
        word_count=len(text.split()),
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def store_result(
    doc: ExtractedDocument,
    bucket: str,
) -> str:
    """Persist the extracted document to object storage.

    Args:
        doc: The fully processed document.
        bucket: Target storage bucket.

    Returns:
        The storage path where the document was saved.
    """
    path = f"s3://{bucket}/{doc.document_id}.json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.put(
            f"https://storage.example.com/{bucket}/{doc.document_id}",
            json=doc.model_dump(),
        )
    return path


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="doc_extraction", version="1")
async def doc_extraction(
    ctx: Context,
    config: DocConfig,
) -> ExtractedDocument:
    """Download a document, extract text, then parse and summarize in parallel.

    Pipeline: download -> extract text -> (AI parse | summarize) -> store.
    """
    raw = await ctx.step(download_attachment, config.document_url)
    text = await ctx.step(extract_text, raw)

    # Run AI parsing and summarization in parallel
    parsed, summary = await ctx.gather(
        ctx.step(ai_parse_document, text, config.openai_api_key),
        ctx.step(summarize_document, text, config.openai_api_key),
    )

    doc = ExtractedDocument(
        document_id=config.document_id,
        raw_text=text[:500],
        parsed=parsed,
        summary=summary,
        storage_path="",
    )

    path = await ctx.step(store_result, doc, config.storage_bucket)
    return doc.model_copy(update={"storage_path": path})
