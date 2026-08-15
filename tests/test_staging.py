"""Per-run artifact staging."""

from __future__ import annotations

from pathlib import Path

import pytest

from loom import Context, ExecutionStatus, Runtime, workflow
from loom.blobs.attachment import Attachment
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.blobs.retention import RetentionManager, RetentionPolicy
from loom.blobs.staging import StagingManager, StagingNotFound
from loom.stores.memory import MemoryStore


@pytest.fixture
def blobs(tmp_path: Path) -> BlobService:
    return BlobService(LocalBlobBackend(tmp_path / "blobs"))


@pytest.fixture
def runtime(blobs: BlobService) -> Runtime:
    return Runtime(store=MemoryStore(), blobs=blobs)


class TestStagingManager:
    async def test_stage_commit_round_trip(self, runtime: Runtime) -> None:
        staging: StagingManager = runtime.staging
        staged = await staging.stage(
            "report.md", b"# hi", mime="text/markdown", run_id="run_a"
        )
        assert staged.name == "report.md"
        assert staged.ref.startswith("blob:")
        version = await staging.commit("report.md", run_id="run_a", labels={"k": "v"})
        assert version.qualified_name == "report.md@1"
        assert version.labels == {"k": "v"}
        assert await staging.get_staged("report.md", run_id="run_a") is None

    async def test_runs_do_not_collide(self, runtime: Runtime) -> None:
        staging = runtime.staging
        await staging.stage("same.txt", b"one", run_id="r1")
        await staging.stage("same.txt", b"two", run_id="r2")
        a = await staging.commit("same.txt", run_id="r1")
        b = await staging.commit("same.txt", run_id="r2")
        assert a.sha256 != b.sha256
        assert b.version == 2

    async def test_offloaded_attachment_reuses_ref(self, runtime: Runtime) -> None:
        att = Attachment.from_bytes("mail.pdf", b"%PDF")
        offloaded = await att.offload(runtime.blobs)
        assert offloaded.is_offloaded
        staged = await runtime.staging.stage("invoice.pdf", offloaded, run_id="r")
        assert staged.ref == offloaded.ref

    async def test_restage_last_write_wins(self, runtime: Runtime) -> None:
        staging = runtime.staging
        await staging.stage("n.txt", b"old", run_id="r")
        await staging.stage("n.txt", b"new", run_id="r")
        version = await staging.commit("n.txt", run_id="r")
        assert await runtime.artifacts.read("n.txt") == b"new"
        assert version.size == 3

    async def test_discard_is_noop_when_missing(self, runtime: Runtime) -> None:
        await runtime.staging.discard("nope", run_id="r")

    async def test_commit_missing_raises(self, runtime: Runtime) -> None:
        with pytest.raises(StagingNotFound):
            await runtime.staging.commit("nope", run_id="r")

    async def test_list_staged(self, runtime: Runtime) -> None:
        staging = runtime.staging
        await staging.stage("a.txt", b"a", run_id="r")
        await staging.stage("b.txt", b"b", run_id="r")
        names = {item.name for item in await staging.list_staged(run_id="r")}
        assert names == {"a.txt", "b.txt"}


class TestStagingContext:
    async def test_stage_and_commit_through_ctx(self, runtime: Runtime) -> None:
        @workflow(name="stage_flow")
        async def flow(ctx: Context, _input: str) -> str:
            await ctx.stage_artifact("note.txt", b"hello", mime="text/plain")
            version = await ctx.commit_staged("note.txt")
            return version.qualified_name

        result = await runtime.run(flow, "go")
        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "note.txt@1"
        assert await runtime.artifacts.read("note.txt") == b"hello"

    async def test_stage_replay_does_not_restage(self, runtime: Runtime) -> None:
        @workflow(name="stage_replay")
        async def flow(ctx: Context, _input: str) -> str:
            staged = await ctx.stage_artifact("x.txt", b"once")
            version = await ctx.commit_staged("x.txt")
            return f"{staged.sha256[:8]}:{version.version}"

        first = await runtime.run(flow, "go")
        replayed = await runtime.replay(first.run_id)
        assert replayed.output == first.output
        history = await runtime.artifacts.history("x.txt")
        assert len(history) == 1

    async def test_discard_through_ctx(self, runtime: Runtime) -> None:
        @workflow(name="discard_flow")
        async def flow(ctx: Context, _input: str) -> str:
            await ctx.stage_artifact("tmp.txt", b"x")
            await ctx.discard_staged("tmp.txt")
            return "ok"

        result = await runtime.run(flow, "go")
        assert result.status is ExecutionStatus.COMPLETED
        assert await runtime.staging.get_staged("tmp.txt", run_id=result.run_id) is None

    async def test_commit_nothing_staged_fails(self, runtime: Runtime) -> None:
        @workflow(name="commit_missing")
        async def flow(ctx: Context, _input: str) -> str:
            await ctx.commit_staged("ghost.txt")
            return "unreachable"

        result = await runtime.run(flow, "go")
        assert result.status is ExecutionStatus.FAILED
        assert "nothing staged" in (result.error.message or "")


class TestRetentionDropsStaging:
    async def test_compact_clears_staging_keys(self, runtime: Runtime) -> None:
        from datetime import UTC, datetime, timedelta

        @workflow(name="to_compact")
        async def flow(ctx: Context, _input: str) -> str:
            await ctx.stage_artifact("left.txt", b"x")
            return "done"

        result = await runtime.run(flow, "go")
        record = await runtime.store.get_execution(result.run_id)
        assert record is not None
        record.finished_at = datetime.now(UTC) - timedelta(days=400)
        await runtime.store.update_execution(record)

        policy = RetentionPolicy(run_record_days=1, journal_warm_days=1)
        await RetentionManager(policy).compact(runtime.store, blobs=runtime.blobs)
        assert await runtime.store.get(f"staging:{result.run_id}:__manifest__") in (
            None,
            [],
        )
