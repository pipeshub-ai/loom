"""Retrieval primitives — the ports, the reference store, and the three nodes.

The failure this whole area exists to prevent is a search that always returns
something. With ``top_k=5`` against an index of six unrelated documents, five of
them come back, and only the score says they are all wrong — so an
unthresholded RAG answer is the model citing the least-bad row, confidently.
Every threshold, every score, and the conformance kit's ranking checks are
about that one thing.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.core.exceptions import ConfigurationError
from loom.knowledge import (
    Chunk,
    MockEmbeddings,
    StoreBackedVectorStore,
    cosine,
    embed_in_batches,
    normalise,
    split_text,
)
from loom.nodes.knowledge import ChunkIn, IndexIn, SearchIn
from loom.stores.memory import MemoryStore
from loom.testing.conformance import verify_vector_store


def unit(*components: float) -> list[float]:
    return normalise(list(components))


# ---------------------------------------------------------------------------
# The maths, and the two cases it must not guess at
# ---------------------------------------------------------------------------


class TestSimilarity:
    def test_identical_vectors_score_one(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_opposite_vectors_score_minus_one(self) -> None:
        assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_a_zero_vector_is_not_similar_rather_than_an_error(self) -> None:
        """A vector with no direction has no similarity to anything.

        Raising here would surface several frames from whatever produced the
        empty embedding.
        """
        assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_mismatched_dimensions_raise(self) -> None:
        """Two embedding models produce two different spaces.

        Comparing across them yields a plausible number that means nothing, so
        this is a configuration error worth finding at the first query.
        """
        with pytest.raises(ValueError, match="different spaces"):
            cosine([1.0, 0.0], [1.0, 0.0, 0.0])

    def test_normalising_a_zero_vector_leaves_it_alone(self) -> None:
        assert normalise([0.0, 0.0]) == [0.0, 0.0]

    def test_normalising_makes_unit_length(self) -> None:
        assert cosine(normalise([3.0, 4.0]), [0.6, 0.8]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Chunking — a rule, and the overlap that makes it work
# ---------------------------------------------------------------------------


class TestChunking:
    def test_short_text_is_one_chunk(self) -> None:
        assert len(split_text("hello", size=100, overlap=10)) == 1

    def test_empty_text_is_no_chunks(self) -> None:
        assert split_text("   \n  ", size=100, overlap=10) == []

    def test_no_chunk_exceeds_the_size(self) -> None:
        """A chunk past the embedding model's window is silently truncated by
        the model, which is worse than an ugly split."""
        text = "word " * 500
        for chunk in split_text(text, size=200, overlap=40):
            assert len(chunk.text) <= 200

    def test_a_fact_spanning_a_boundary_survives_whole(self) -> None:
        """The whole reason overlap exists.

        Split cleanly, a fact straddling a boundary is in neither chunk — so a
        query for it matches nothing and the run reports "not found" about a
        document that plainly says it.
        """
        filler = "x" * 190
        text = f"{filler} THE NOTICE PERIOD IS NINETY DAYS {filler}"
        chunks = split_text(text, size=200, overlap=80)
        assert any("THE NOTICE PERIOD IS NINETY DAYS" in c.text for c in chunks)

    def test_it_prefers_a_paragraph_boundary(self) -> None:
        """Within the lookback window — not anywhere at all.

        A break in the first fifth of the window is *not* preferred: cutting
        there would leave a chunk a fraction of the budget, and the overlap
        stops covering the gaps once the chunks are that uneven. So the
        boundary here sits near the end of the window, which is where the
        preference is worth having.
        """
        head = "sentence one. " * 6  # ~84 chars, inside the lookback
        text = head + "\n\n" + "tail text " * 30
        chunks = split_text(text, size=100, overlap=20)
        assert chunks[0].text.endswith("sentence one.")

    def test_a_boundary_too_early_is_not_taken(self) -> None:
        """The other half of the rule, so the lookback is not accidental."""
        text = "tiny.\n\n" + "filler " * 60
        chunks = split_text(text, size=120, overlap=20)
        assert not chunks[0].text.endswith("tiny.")
        assert len(chunks[0].text) > 100

    def test_chunks_are_ordered(self) -> None:
        chunks = split_text("word " * 400, size=200, overlap=40)
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))

    def test_the_same_text_produces_the_same_ids(self) -> None:
        """What makes a re-ingest an update rather than a duplication."""
        first = split_text("hello world " * 50, size=100, overlap=20, source="doc")
        second = split_text("hello world " * 50, size=100, overlap=20, source="doc")
        assert [c.id for c in first] == [c.id for c in second]

    def test_a_different_source_produces_different_ids(self) -> None:
        one = split_text("same text", size=100, overlap=10, source="a")
        two = split_text("same text", size=100, overlap=10, source="b")
        assert one[0].id != two[0].id

    def test_overlap_at_or_above_size_is_refused(self) -> None:
        """Otherwise each window starts where the last one did and the split
        never terminates — a hang rather than a bad answer."""
        with pytest.raises(ValueError, match="would not"):
            split_text("hello", size=100, overlap=100)

    def test_a_pathological_document_still_terminates(self) -> None:
        """No spaces, no punctuation — nothing to cut on."""
        chunks = split_text("a" * 5000, size=100, overlap=20)
        assert len(chunks) > 1
        assert "".join(dict.fromkeys("".join(c.text for c in chunks)))


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class TestEmbeddings:
    @pytest.mark.asyncio()
    async def test_the_mock_is_deterministic(self) -> None:
        provider = MockEmbeddings()
        first = await provider.embed_query("hello")
        second = await provider.embed_query("hello")
        assert first == second

    @pytest.mark.asyncio()
    async def test_different_text_embeds_differently(self) -> None:
        provider = MockEmbeddings()
        assert await provider.embed_query("a") != await provider.embed_query("b")

    @pytest.mark.asyncio()
    async def test_batching_preserves_order(self) -> None:
        """A provider returning them shuffled would pair every chunk with
        another one's meaning, and a caller cannot detect it."""
        provider = MockEmbeddings()
        texts = [f"text {n}" for n in range(10)]
        batched = await embed_in_batches(provider, texts, batch_size=3)
        one_at_a_time = [await provider.embed_query(t) for t in texts]
        assert batched == one_at_a_time

    @pytest.mark.asyncio()
    async def test_no_texts_costs_no_request(self) -> None:
        class Counting(MockEmbeddings):
            calls = 0

            async def embed_documents(self, texts: Any) -> Any:
                type(self).calls += 1
                return await super().embed_documents(texts)

        assert await embed_in_batches(Counting(), []) == []
        assert Counting.calls == 0

    @pytest.mark.asyncio()
    async def test_a_short_answer_is_refused(self) -> None:
        """Silently mis-aligning every chunk after the gap is the alternative."""

        class Short(MockEmbeddings):
            async def embed_documents(self, texts: Any) -> Any:
                return (await super().embed_documents(texts))[:-1]

        with pytest.raises(ConfigurationError, match="mis-align"):
            await embed_in_batches(Short(), ["a", "b"])


# ---------------------------------------------------------------------------
# The reference store, through the conformance kit
# ---------------------------------------------------------------------------


class TestStoreBackedVectorStore:
    @pytest.mark.asyncio()
    async def test_it_conforms(self) -> None:
        """One shared backing store across every factory call, which is what
        proves an index outlives the handle that wrote it."""
        backing = MemoryStore()
        await verify_vector_store(lambda: StoreBackedVectorStore(backing))

    @pytest.mark.asyncio()
    async def test_a_chunk_and_vector_count_mismatch_is_refused(self) -> None:
        store = StoreBackedVectorStore(MemoryStore())
        with pytest.raises(ValueError, match="another one's meaning"):
            await store.upsert("ns", [Chunk(text="a"), Chunk(text="b")], [unit(1, 0)], model="m")

    @pytest.mark.asyncio()
    async def test_ids_are_derived_when_absent(self) -> None:
        store = StoreBackedVectorStore(MemoryStore())
        await store.upsert("ns", [Chunk(text="a", source="s")], [unit(1, 0)], model="m")
        await store.upsert("ns", [Chunk(text="a", source="s")], [unit(1, 0)], model="m")
        assert await store.count("ns") == 1

    @pytest.mark.asyncio()
    async def test_a_query_from_a_second_model_is_refused(self) -> None:
        store = StoreBackedVectorStore(MemoryStore())
        await store.upsert("ns", [Chunk(text="a")], [unit(1, 0)], model="m1")
        with pytest.raises(ConfigurationError, match="meaningless"):
            await store.query("ns", unit(1, 0), model="m2")

    @pytest.mark.asyncio()
    async def test_ties_break_deterministically(self) -> None:
        """An unstable order across identical queries makes a replayed run cite
        a different chunk, which is a divergence nothing else would flag."""
        store = StoreBackedVectorStore(MemoryStore())
        await store.upsert(
            "ns",
            [Chunk(id="b", text="b"), Chunk(id="a", text="a"), Chunk(id="c", text="c")],
            [unit(1, 0), unit(1, 0), unit(1, 0)],
            model="m",
        )
        first = [m.chunk.id for m in await store.query("ns", unit(1, 0), top_k=3, model="m")]
        second = [m.chunk.id for m in await store.query("ns", unit(1, 0), top_k=3, model="m")]
        assert first == second == ["a", "b", "c"]


class TestTheConformanceKitBites:
    """A kit that passes anything proves nothing.

    Each of these is a store broken in exactly one way the kit exists to
    catch — the same check ``tests/conformance/test_harness.py`` makes of the
    store suite.
    """

    def _broken(self, **overrides: Any) -> Any:
        backing = MemoryStore()

        class Broken(StoreBackedVectorStore):
            pass

        for name, fn in overrides.items():
            setattr(Broken, name, fn)
        return lambda: Broken(backing)

    @pytest.mark.asyncio()
    async def test_it_catches_an_upsert_that_appends(self) -> None:
        async def appending(
            self: Any, namespace: str, chunks: Any, vectors: Any, *, model: str
        ) -> int:
            # Breaks exactly one property: a *first* write keeps its id, and a
            # re-write of the same id lands beside the original instead of
            # replacing it. Renaming unconditionally would fail the round-trip
            # check first, which would prove the kit bites somewhere rather
            # than that it catches this.
            rows = await self._read(namespace)
            renamed = [
                c.model_copy(update={"id": f"{c.id}-dup"})
                if c.with_derived_id().id in rows
                else c
                for c in chunks
            ]
            return await StoreBackedVectorStore.upsert(
                self, namespace, renamed, vectors, model=model
            )

        with pytest.raises(AssertionError, match="doubles the index"):
            await verify_vector_store(self._broken(upsert=appending))

    @pytest.mark.asyncio()
    async def test_it_catches_a_filter_applied_after_top_k(self) -> None:
        async def late_filter(
            self: Any, namespace: str, vector: Any, *, top_k: int = 5,
            where: Any = None, model: str = "",
        ) -> Any:
            found = await StoreBackedVectorStore.query(
                self, namespace, vector, top_k=top_k, where=None, model=model
            )
            if where:
                found = [
                    m for m in found
                    if all(m.chunk.metadata.get(k) == v for k, v in where.items())
                ]
            return found

        with pytest.raises(AssertionError, match="narrow before top_k"):
            await verify_vector_store(self._broken(query=late_filter))

    @pytest.mark.asyncio()
    async def test_it_catches_an_index_that_accepts_two_models(self) -> None:
        async def permissive(
            self: Any, namespace: str, chunks: Any, vectors: Any, *, model: str
        ) -> int:
            return await StoreBackedVectorStore.upsert(
                self, namespace, chunks, vectors, model=""
            )

        with pytest.raises(AssertionError, match="second embedding model"):
            await verify_vector_store(self._broken(upsert=permissive))


# ---------------------------------------------------------------------------
# The nodes, end to end
# ---------------------------------------------------------------------------


def knowledge_runtime() -> Any:
    from loom import Runtime

    backing = MemoryStore()
    return Runtime(
        store=backing,
        embeddings=MockEmbeddings(),
        vectors=StoreBackedVectorStore(backing),
    )


class TestNodes:
    @pytest.mark.asyncio()
    async def test_chunk_index_search_round_trip(self) -> None:
        from loom import Context, workflow

        @workflow(name="rag_round_trip")
        async def flow(ctx: Context, _: None = None) -> dict[str, Any]:
            chunked = await ctx.node(
                "knowledge.chunk",
                ChunkIn(
                    text=(
                        "The notice period is ninety days.\n\n"
                        "Payment terms are net thirty.\n\n"
                        "Governing law is England and Wales."
                    ),
                    size=60,
                    overlap=10,
                    source="contract",
                ),
            )
            indexed = await ctx.node(
                "knowledge.index",
                IndexIn(namespace="contract", chunks=chunked.chunks),
            )
            # Queried with a chunk's *exact* text. MockEmbeddings hashes, so
            # it captures no meaning — "notice period" and the chunk saying so
            # are as far apart here as any two strings. Exact text is the one
            # similarity it can honestly assert, and asserting more would pass
            # or fail on hash luck.
            found = await ctx.node(
                "knowledge.search",
                SearchIn(
                    namespace="contract",
                    query=chunked.chunks[0].text,
                    top_k=1,
                ),
            )
            return {
                "chunks": chunked.count,
                "indexed": indexed.indexed,
                "top": found.matches[0].chunk.text if found.matches else "",
                "score": found.matches[0].score if found.matches else 0.0,
            }

        result = await knowledge_runtime().run(flow)
        assert result.status.value == "completed", result.error
        out = result.output
        assert out["chunks"] >= 2
        assert out["indexed"] == out["chunks"]
        assert "notice period" in out["top"]
        assert out["score"] == pytest.approx(1.0), (
            "an exact-text query must be the nearest match — MockEmbeddings "
            "hashes, so this is the one similarity it can honestly assert"
        )

    @pytest.mark.asyncio()
    async def test_a_threshold_reports_what_it_dropped(self) -> None:
        """"Nothing scored well" and "the index is empty" are different facts."""
        from loom import Context, workflow

        @workflow(name="rag_threshold")
        async def flow(ctx: Context, _: None = None) -> dict[str, int]:
            await ctx.node(
                "knowledge.index",
                IndexIn(
                    namespace="tiny",
                    chunks=[Chunk(text="entirely unrelated content", source="x")],
                ),
            )
            found = await ctx.node(
                "knowledge.search",
                SearchIn(
                    namespace="tiny",
                    query="what is the notice period",
                    top_k=5,
                    min_score=0.99,
                ),
            )
            return {
                "found": found.found,
                "dropped": found.dropped_below_threshold,
            }

        result = await knowledge_runtime().run(flow)
        assert result.status.value == "completed", result.error
        assert result.output == {"found": 0, "dropped": 1}

    @pytest.mark.asyncio()
    async def test_chunking_needs_no_ports(self) -> None:
        """A rule, so it is free on a Runtime with nothing configured."""
        from loom import Context, Runtime, workflow

        @workflow(name="rag_chunk_only")
        async def flow(ctx: Context, _: None = None) -> int:
            out = await ctx.node("knowledge.chunk", ChunkIn(text="hello world"))
            return int(out.count)

        result = await Runtime(store=MemoryStore()).run(flow)
        assert result.status.value == "completed"
        assert result.output == 1

    @pytest.mark.asyncio()
    async def test_indexing_without_ports_says_what_to_pass(self) -> None:
        from loom import Context, Runtime, workflow

        @workflow(name="rag_unconfigured")
        async def flow(ctx: Context, _: None = None) -> int:
            out = await ctx.node("knowledge.index", IndexIn(namespace="x", chunks=[]))
            return int(out.indexed)

        result = await Runtime(store=MemoryStore()).run(flow)
        assert result.status.value == "failed"
        assert result.error is not None
        assert "embeddings" in result.error.message

    @pytest.mark.asyncio()
    async def test_re_indexing_does_not_double_the_namespace(self) -> None:
        """The property that makes an ingest safe to re-run."""
        from loom import Context, workflow

        @workflow(name="rag_reingest")
        async def flow(ctx: Context, _: None = None) -> int:
            chunked = await ctx.node(
                "knowledge.chunk",
                ChunkIn(text="word " * 200, size=100, overlap=20, source="doc"),
            )
            await ctx.node(
                "knowledge.index", IndexIn(namespace="doc", chunks=chunked.chunks)
            )
            await ctx.node(
                "knowledge.index",
                IndexIn(namespace="doc", chunks=chunked.chunks),
                name="reindex",
            )
            return int(chunked.count)

        runtime = knowledge_runtime()
        result = await runtime.run(flow)
        assert result.status.value == "completed", result.error
        assert result.output > 1, "the fixture must produce several chunks"
        assert await runtime.vectors.count("doc") == result.output, (
            "indexing the same chunks twice must leave one row per chunk"
        )


class TestRegistered:
    def test_all_three_are_in_the_catalog(self) -> None:
        from loom.nodes import get_node_catalog, load_builtin_nodes

        load_builtin_nodes()
        ids = set(get_node_catalog().node_ids())
        assert {"knowledge.chunk", "knowledge.index", "knowledge.search"} <= ids

    def test_chunking_is_declared_deterministic(self) -> None:
        """A rule, so it is free to recompute on replay."""
        from loom.nodes.knowledge import ChunkNode, IndexNode, SearchNode

        assert ChunkNode.spec.deterministic is True
        assert IndexNode.spec.deterministic is False
        assert SearchNode.spec.deterministic is False

    def test_the_service_nodes_declare_their_requirements(self) -> None:
        from loom.nodes.knowledge import ChunkNode, IndexNode, SearchNode

        assert set(IndexNode.spec.requires) == {"embeddings", "vectors"}
        assert set(SearchNode.spec.requires) == {"embeddings", "vectors"}
        assert ChunkNode.spec.requires == []

    def test_indexing_is_a_write_and_searching_is_a_read(self) -> None:
        from loom.nodes.knowledge import IndexNode, SearchNode

        assert IndexNode.spec.effect.value == "write"
        assert SearchNode.spec.effect.value == "read"
