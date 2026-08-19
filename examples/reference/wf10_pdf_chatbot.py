"""Workflow: RAG Chat with a PDF.

Ingest a document once, then answer questions about it in a durable loop — the
run parks between questions at no cost and survives a restart mid-conversation.

What this shows, and why each part is the way it is:

* **This is the workflow LOOM could not express.** The version it replaces
  faked every step: "extract text" decoded PDF bytes as UTF-8, "embed and
  index" POSTed to an invented host, and "semantic search" returned whatever
  came back. There was no document parser, no embedding port, and no vector
  store in the library at all. All three exist now, which is what makes this
  the *whole* pipeline rather than a shape.

* **A search always returns something, so it is thresholded.** With ``top_k=4``
  against an index of unrelated pages, four come back and only the score says
  they are all wrong. ``min_score`` is what turns that into "I could not find
  it" instead of a confident answer assembled from the least-bad rows — and
  ``dropped_below_threshold`` distinguishes "nothing scored well" from "the
  index is empty".

* **The answer cites what it used.** Every match carries its chunk's source and
  ordinal, so the reply can say where it came from. An unsourced RAG answer is
  the one nobody can check.

* **The loop is bounded by ``continue_as_new``, not by a counter.** Three
  durable calls per question in one journal is what makes a long conversation
  slow to replay and eventually fail the entry budget. Rotating hands the
  successor a clean journal and the same namespace, so the index is not
  rebuilt — which is the pattern this workflow should have been teaching all
  along.

* **The ingest is re-runnable.** Chunk ids are derived from content, so
  re-indexing the same document updates rows instead of doubling the index.
  That is what makes a rotated run safe to re-enter the ingest branch.

Credentials: ``GOOGLE_*`` (Drive, to fetch the PDF), ``SLACK_BOT_TOKEN``, and
whatever the embedding provider needs. Reading a PDF needs
``pip install 'loomflow[documents]'``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, Retry, step, workflow
from loom.knowledge import Chunk
from loom.nodes.documents import ParseDocumentIn
from loom.nodes.knowledge import ChunkIn, IndexIn, SearchIn
from loom.security.grants import GrantSet
from loom.toolsets.google.drive.tools import drive_download_file
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Manual

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ChatbotConfig(BaseModel):
    """One document, and where the conversation happens."""

    file_id: str = Field(description="Drive file id of the PDF to answer about.")
    session_id: str = Field(description="Names this conversation, and its index.")
    channel: str = "#doc-qa"
    chunk_size: int = 1000
    chunk_overlap: int = 150
    top_k: int = 4
    min_score: float = 0.15
    """Below this, a match is not evidence.

    Cosine similarity, so the useful range depends on the embedding model —
    this is a starting point to tune, not a constant. Zero would accept
    anything the index happens to contain."""

    questions_per_run: int = 20
    """How many questions before ``continue_as_new``.

    Not a limit on the conversation — a limit on one journal. The successor
    carries the same namespace, so nothing is re-indexed."""

    questions_answered: int = 0
    """Carried across rotations, so the reported total is the conversation's
    rather than this segment's."""

    indexed: int = 0
    """Chunks already in the namespace. Non-zero means a successor run, which
    is how the ingest branch is skipped."""


class ChatMessage(BaseModel):
    """One question from a person."""

    question: str
    asked_by: str = ""


class ChatResult(BaseModel):
    """What the workflow returns when the conversation ends."""

    session_id: str
    chunks_indexed: int = 0
    questions_answered: int = 0
    unanswered: int = 0
    """Questions where every match fell below the threshold.

    Reported rather than hidden: a chatbot that answered nine of ten is a
    different thing from one that answered ten."""

    ended_by: str = ""
    """``end``, ``timeout``, or ``rotated``."""


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def fetch_document(file_id: str) -> object:
    """The PDF's bytes, as an Attachment carrying its own filename and MIME.

    ``drive_download_file`` rather than ``drive_export_file``: this is a real
    PDF with bytes. A *Google Doc* has none and must be exported — downloading
    one is a 403 that reads as a permissions problem.
    """
    return await drive_download_file(file_id=file_id)


@step(retry=Retry(max_attempts=1))
async def reply(channel: str, question: str, answer: str, sources: list[str]) -> str:
    """Post the answer with its citations. Not retried — a retry posts twice."""
    cited = "\n".join(f"> {source}" for source in sources)
    posted = await slack_post_message(
        channel=channel,
        text=f"*{question}*\n\n{answer}" + (f"\n\n_Sources_\n{cited}" if cited else ""),
    )
    return posted.ts


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="pdf_chatbot",
    version="2",
    triggers=[Manual()],
    grants=GrantSet(toolsets=["google_drive", "slack"]),
)
async def pdf_chatbot(ctx: Context, config: ChatbotConfig) -> ChatResult:
    """Ingest a document once, then answer questions about it until told to stop."""
    indexed = config.indexed

    # Skipped on a successor run: the namespace already holds the document, and
    # re-indexing would be correct but wasteful. Chunk ids are derived from
    # content, so doing it anyway would update rather than duplicate — the
    # branch is an optimisation, not a correctness requirement.
    if not indexed:
        document = await ctx.step(fetch_document, config.file_id)
        parsed = await ctx.node(
            "transform.parse_document", ParseDocumentIn(document=document)
        )
        chunked = await ctx.node(
            "knowledge.chunk",
            ChunkIn(
                text=parsed.text,
                size=config.chunk_size,
                overlap=config.chunk_overlap,
                source=config.file_id,
            ),
        )
        stored = await ctx.node(
            "knowledge.index",
            IndexIn(namespace=config.session_id, chunks=chunked.chunks),
        )
        indexed = stored.indexed
        await ctx.step(
            reply,
            config.channel,
            "Ready",
            f"Indexed {indexed} chunks from {parsed.filename or config.file_id}"
            + (
                f" (only {len(parsed.pages)} of {parsed.page_count} pages were read)"
                if parsed.truncated
                else ""
            ),
            [],
        )

    answered = config.questions_answered
    unanswered = 0

    for _ in range(config.questions_per_run):
        message: ChatMessage | None = await ctx.wait_for_event(
            "user_question",
            timeout=3600,
            default=None,
            # Without this the payload arrives as the raw dict it was delivered
            # as, and `message.question` below raises — only once a question
            # actually lands, so a run that times out looks fine.
            output_type=ChatMessage,
        )
        if message is None:
            return ChatResult(
                session_id=config.session_id,
                chunks_indexed=indexed,
                questions_answered=answered,
                unanswered=unanswered,
                ended_by="timeout",
            )
        if message.question.strip().lower() == "end":
            return ChatResult(
                session_id=config.session_id,
                chunks_indexed=indexed,
                questions_answered=answered,
                unanswered=unanswered,
                ended_by="end",
            )

        found = await ctx.node(
            "knowledge.search",
            SearchIn(
                namespace=config.session_id,
                query=message.question,
                top_k=config.top_k,
                min_score=config.min_score,
            ),
        )

        if not found.matches:
            # Nothing cleared the bar. Saying so is the whole point of the
            # threshold — the alternative is a fluent answer assembled from
            # rows the score already said were irrelevant.
            await ctx.step(
                reply,
                config.channel,
                message.question,
                "I could not find anything in this document that answers that."
                + (
                    f" ({found.dropped_below_threshold} passage(s) were close "
                    "but not close enough.)"
                    if found.dropped_below_threshold
                    else ""
                ),
                [],
            )
            unanswered += 1
            continue

        context = "\n\n---\n\n".join(
            f"[{_cite(match.chunk)}]\n{match.chunk.text}" for match in found.matches
        )
        answer = await ctx.agent(
            "Answer the question using only the passages below. Quote the "
            "bracketed citation for anything you assert. If the passages do "
            "not answer it, say so rather than filling the gap.\n\n"
            f"Question: {message.question}\n\nPassages:\n{context}",
            name="answer",
        )

        await ctx.step(
            reply,
            config.channel,
            message.question,
            str(answer.output),
            [_cite(match.chunk) for match in found.matches],
        )
        answered += 1

    # The journal, not the conversation, is what is bounded. The successor
    # keeps the namespace and the running total, so nothing is re-indexed and
    # the count a person sees is the conversation's.
    await ctx.continue_as_new(
        config.model_copy(
            update={"questions_answered": answered, "indexed": indexed}
        )
    )
    return ChatResult(
        session_id=config.session_id,
        chunks_indexed=indexed,
        questions_answered=answered,
        unanswered=unanswered,
        ended_by="rotated",
    )


def _cite(chunk: Chunk) -> str:
    """How one chunk is referred to in an answer.

    A pure function, so it needs no journal entry — and a citation a reader can
    act on, rather than an opaque id.
    """
    where = chunk.source or "document"
    return f"{where} #{chunk.ordinal + 1}"
