"""Workflow: RAG Chat with PDF."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from loom import Context, Retry, step, workflow

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChatbotConfig(BaseModel):
    """Input configuration for the PDF chatbot."""

    pdf_url: str
    session_id: str
    chunk_size: int = 500
    chunk_overlap: int = 50
    max_questions: int = 20
    openai_api_key: str = ""


class TextChunk(BaseModel):
    """A chunk of text with its embedding."""

    chunk_id: int
    text: str
    embedding: list[float] = []


class SearchResult(BaseModel):
    """A search result from the vector store."""

    chunk_id: int
    text: str
    score: float = 0.0


class ChatMessage(BaseModel):
    """A chat message from the user."""

    question: str
    session_id: str


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, delay=2.0))
async def extract_text(pdf_url: str) -> str:
    """Download and extract text from a PDF.

    Args:
        pdf_url: URL of the PDF to process.

    Returns:
        Extracted plain text.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(pdf_url)
        resp.raise_for_status()
        raw = resp.content

    # In production, use PyPDF2 or pdfplumber
    return raw.decode("utf-8", errors="replace")


@step
async def chunk_text(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[TextChunk]:
    """Split text into overlapping chunks for embedding.

    Args:
        text: Full document text.
        chunk_size: Characters per chunk.
        chunk_overlap: Overlap between chunks.

    Returns:
        List of text chunks.
    """
    chunks: list[TextChunk] = []
    start = 0
    chunk_id = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(TextChunk(
            chunk_id=chunk_id,
            text=text[start:end],
        ))
        chunk_id += 1
        start += chunk_size - chunk_overlap
        if start >= len(text):
            break
    return chunks


@step(retry=Retry(max_attempts=2, delay=1.0))
async def embed_and_index(
    chunks: list[TextChunk],
    session_id: str,
    api_key: str,
) -> int:
    """Generate embeddings and index chunks in a vector store.

    Args:
        chunks: Text chunks to embed.
        session_id: Session ID for namespacing the index.
        api_key: OpenAI API key.

    Returns:
        Number of chunks indexed.
    """
    batch_size = 20
    indexed = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c.text for c in batch]

            # Get embeddings from OpenAI
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": "text-embedding-3-small",
                    "input": texts,
                },
            )
            resp.raise_for_status()
            data = resp.json()["data"]

            # Store in vector DB
            vectors = []
            for chunk, emb in zip(batch, data, strict=True):
                vectors.append({
                    "id": f"{session_id}-{chunk.chunk_id}",
                    "values": emb["embedding"],
                    "metadata": {"text": chunk.text},
                })

            await client.post(
                "https://vectordb.example.com/api/upsert",
                json={
                    "namespace": session_id,
                    "vectors": vectors,
                },
            )
            indexed += len(batch)

    return indexed


@step(retry=Retry(max_attempts=2, delay=1.0))
async def semantic_search(
    query: str,
    session_id: str,
    api_key: str,
    top_k: int = 5,
) -> list[SearchResult]:
    """Search the vector store for relevant chunks.

    Args:
        query: The user's question.
        session_id: Session namespace.
        api_key: OpenAI API key.
        top_k: Number of results to return.

    Returns:
        Ranked search results.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Embed the query
        emb_resp = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "text-embedding-3-small",
                "input": [query],
            },
        )
        emb_resp.raise_for_status()
        query_vec = emb_resp.json()["data"][0]["embedding"]

        # Search vector DB
        search_resp = await client.post(
            "https://vectordb.example.com/api/query",
            json={
                "namespace": session_id,
                "vector": query_vec,
                "top_k": top_k,
            },
        )
        search_resp.raise_for_status()
        matches = search_resp.json().get("matches", [])

    return [
        SearchResult(
            chunk_id=m.get("id", 0),
            text=m.get("metadata", {}).get("text", ""),
            score=m.get("score", 0.0),
        )
        for m in matches
    ]


@step(retry=Retry(max_attempts=2, delay=1.0))
async def generate_answer(
    question: str,
    context_chunks: list[SearchResult],
    api_key: str,
) -> str:
    """Generate an answer using retrieved context.

    Args:
        question: The user's question.
        context_chunks: Relevant text chunks.
        api_key: OpenAI API key.

    Returns:
        AI-generated answer.
    """
    context_text = "\n---\n".join(
        r.text for r in context_chunks if r.text
    )
    prompt = (
        "Answer the question using only the context below. "
        "If the answer is not in the context, say so.\n\n"
        f"Context:\n{context_text[:3000]}\n\n"
        f"Question: {question}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": "Answer from provided context.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


@step
async def send_response(
    session_id: str,
    question: str,
    answer: str,
) -> bool:
    """Send the chatbot response back to the user.

    Args:
        session_id: Chat session ID.
        question: Original question.
        answer: Generated answer.

    Returns:
        True if response was delivered.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            "https://chat.example.com/api/messages",
            json={
                "session_id": session_id,
                "role": "assistant",
                "content": answer,
                "in_reply_to": question[:100],
            },
        )
    return True


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="pdf_chatbot", version="1")
async def pdf_chatbot(
    ctx: Context,
    config: ChatbotConfig,
) -> dict:
    """Extract a PDF, build a vector index, then chat in a loop.

    Pipeline:
      1. Extract text -> chunk -> embed and index
      2. Loop: wait for question -> search -> generate -> respond
    """
    # Phase 1: Ingest the PDF
    text = await ctx.step(extract_text, config.pdf_url)
    chunks = await ctx.step(
        chunk_text, text, config.chunk_size, config.chunk_overlap,
    )
    chunks_indexed = await ctx.step(
        embed_and_index,
        chunks,
        config.session_id,
        config.openai_api_key,
    )

    # Phase 2: Chat loop -- wait for questions
    questions_answered = 0

    for _ in range(config.max_questions):
        # Park until a user sends a question
        message: ChatMessage | None = await ctx.wait_for_event(
            "user_question",
            timeout=3600,
            default=None,
        )

        if message is None:
            break  # Timed out waiting for a question

        # Retrieve relevant chunks
        results = await ctx.step(
            semantic_search,
            message.question,
            config.session_id,
            config.openai_api_key,
        )

        # Generate answer from context
        answer = await ctx.step(
            generate_answer,
            message.question,
            results,
            config.openai_api_key,
        )

        # Send response
        await ctx.step(
            send_response,
            config.session_id,
            message.question,
            answer,
        )
        questions_answered += 1

    return {
        "session_id": config.session_id,
        "chunks_indexed": chunks_indexed,
        "questions_answered": questions_answered,
    }
