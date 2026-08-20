"""A journal entry is bounded by bytes, not only by count.

`journal_max_entries` counts *entries*, so one enormous entry is `1` to it. With
no `BlobService` configured — which is the default — nothing sized a payload at
all: a step returning 200 MB was written to the journal verbatim. What happened
next depended entirely on the backend, which is the tell that nothing in LOOM
was deciding. Mongo raised an opaque `DocumentTooLarge` from inside a
`bulk_write` at 16 MB; SQLite and Postgres accepted it and produced a row that
is re-read and re-parsed on every subsequent replay — degrading exactly the way
the entry budget exists to prevent, while being invisible to it.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def big(size: int) -> str:
    return "x" * size


@workflow(name="fat")
async def fat(ctx: Context, size: int) -> int:
    return len(await ctx.step(big, size))


class TestPayloadCeiling:
    async def test_an_oversized_payload_fails_the_run_with_a_named_fix(self) -> None:
        rt = Runtime(store=MemoryStore(), journal_max_payload_bytes=50_000)
        rt.register(fat)
        try:
            result = await rt.run(fat, 200_000)
        finally:
            await rt.shutdown()

        assert result.error is not None
        assert result.error.type == "BudgetExceeded"
        # Actionable rather than merely refusing: the message has to name the
        # thing that makes large payloads work.
        assert "BlobService" in result.error.message

    async def test_an_ordinary_payload_is_untouched(self) -> None:
        """The negative control — a guard that fires on normal work is noise."""
        rt = Runtime(store=MemoryStore(), journal_max_payload_bytes=50_000)
        rt.register(fat)
        try:
            result = await rt.run(fat, 100)
        finally:
            await rt.shutdown()

        assert result.output == 100

    async def test_zero_disables_it(self) -> None:
        """Matching `journal_max_entries`, whose 0 means the same thing."""
        rt = Runtime(store=MemoryStore(), journal_max_payload_bytes=0)
        rt.register(fat)
        try:
            result = await rt.run(fat, 200_000)
        finally:
            await rt.shutdown()

        assert result.output == 200_000

    async def test_the_warning_fires_once(self, caplog) -> None:
        import logging

        rt = Runtime(
            store=MemoryStore(),
            journal_warn_payload_bytes=1_000,
            journal_max_payload_bytes=0,
        )
        rt.register(fat)
        with caplog.at_level(logging.WARNING):
            try:
                await rt.run(fat, 5_000)
            finally:
                await rt.shutdown()

        assert any("no blob service" in r.message for r in caplog.records)

    async def test_a_blob_service_takes_it_out_of_the_journal_instead(
        self, tmp_path
    ) -> None:
        """The fix the error names, working.

        With blobs configured the payload never reaches the size check — it
        becomes a `blob:` reference — so a limit far below the payload is not
        merely tolerated, it is irrelevant.
        """
        from loom.blobs.blob import BlobService, blob_backend_from_url

        rt = Runtime(
            store=MemoryStore(),
            blobs=BlobService(blob_backend_from_url(f"file://{tmp_path}"), threshold=1_000),
            journal_max_payload_bytes=50_000,
        )
        rt.register(fat)
        try:
            result = await rt.run(fat, 200_000)
        finally:
            await rt.shutdown()

        assert result.output == 200_000

    def test_the_threshold_is_configurable_without_monkeypatching(self, tmp_path) -> None:
        """It was a class attribute only, so tuning it meant patching the class."""
        from loom.blobs.blob import BlobService, blob_backend_from_url

        backend = blob_backend_from_url(f"file://{tmp_path}")

        assert BlobService(backend, threshold=10).should_offload(b"x" * 11)
        assert not BlobService(backend, threshold=10_000).should_offload(b"x" * 11)
        assert BlobService(backend).threshold == BlobService.OFFLOAD_THRESHOLD


class TestMongoDocumentCeiling:
    """Mongo's limit is the server's, so the only question is what the caller learns."""

    def test_an_oversized_document_is_refused_with_context(self) -> None:
        pytest.importorskip("bson")
        from loom.stores.mongo import BSON_DOCUMENT_LIMIT, _refuse_oversized

        doc = {"run_id": "r1", "path": "0", "data": {"x": "y" * (BSON_DOCUMENT_LIMIT + 1)}}

        with pytest.raises(ValueError) as caught:
            _refuse_oversized(doc, "r1", "0")

        message = str(caught.value)
        # Everything a raw DocumentTooLarge does not say.
        assert "r1" in message
        assert "BlobService" in message
        assert str(BSON_DOCUMENT_LIMIT) in message.replace(",", "")

    def test_an_ordinary_document_passes(self) -> None:
        pytest.importorskip("bson")
        from loom.stores.mongo import _refuse_oversized

        _refuse_oversized({"run_id": "r1", "path": "0", "data": {"x": "y"}}, "r1", "0")


class TestAgentItemsAreBoundedToo:
    """`ResultBounds` capped the model's view and nothing else.

    `messages` and `items` both live inside the one `AgentResult` that becomes
    one journal entry, so keeping the full text in `items` meant twenty 4 MB
    tool calls still concentrated ~80 MB into a single row — the likeliest real
    path to an oversized entry.
    """

    def test_the_item_carries_the_bounded_text_when_bounds_are_set(self) -> None:
        import inspect

        from loom.agents import runner

        source = inspect.getsource(runner)

        assert "content=text.bounded if agent.bounds is not None else text.raw" in source
