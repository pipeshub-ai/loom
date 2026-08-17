"""Coverage for attachments, versioned artifacts, orphan recovery, the CLI, and
the visualization pipeline.

Everything runs in-process: ``MemoryStore`` for state, ``LocalBlobBackend`` in a
tmp dir for content, and subprocesses only where the feature under test *is* a
subprocess. No network, no servers.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.blobs.artifact import (
    ArtifactNotFound,
    ArtifactService,
    InMemoryArtifactStore,
    StoreBackedArtifactStore,
)
from loom.blobs.attachment import Attachment
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.stores.memory import MemoryStore


@pytest.fixture
def blobs(tmp_path: Path) -> BlobService:
    return BlobService(LocalBlobBackend(tmp_path / "blobs"))


@pytest.fixture
def artifacts(blobs: BlobService) -> ArtifactService:
    return ArtifactService(blobs, InMemoryArtifactStore())


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


class TestAttachment:
    def test_from_bytes_infers_mime_and_size(self) -> None:
        att = Attachment.from_bytes("report.pdf", b"%PDF-1.7 ...")

        assert att.mime == "application/pdf"
        assert att.size == 12
        assert att.sha256

    def test_unknown_extension_falls_back(self) -> None:
        assert Attachment.from_bytes("blob.zzz", b"x").mime == "application/octet-stream"

    def test_explicit_mime_wins(self) -> None:
        att = Attachment.from_bytes("data.bin", b"x", mime="image/png")
        assert att.mime == "image/png"

    def test_from_text_round_trips(self) -> None:
        att = Attachment.from_text("notes.txt", "héllo")
        assert att.text() == "héllo"
        assert att.mime == "text/plain"

    def test_from_path(self, tmp_path: Path) -> None:
        target = tmp_path / "data.csv"
        target.write_text("a,b\n1,2\n")

        att = Attachment.from_path(target)
        assert att.filename == "data.csv"
        assert att.mime == "text/csv"
        assert att.text().startswith("a,b")

    def test_metadata_is_carried(self) -> None:
        att = Attachment.from_bytes("x.txt", b"x", source="inbox")
        assert att.metadata == {"source": "inbox"}

    async def test_survives_a_journal_round_trip(self) -> None:
        """Binary must come back byte-identical, not merely truthy."""
        from loom.core.serde import decode, encode

        payload = bytes(range(256))
        att = Attachment.from_bytes("blob.bin", payload)

        restored = decode(encode(att), Attachment)
        assert isinstance(restored, Attachment)
        assert restored.data == payload
        assert restored.sha256 == att.sha256

    async def test_offload_moves_bytes_to_blob_storage(self, blobs: BlobService) -> None:
        att = Attachment.from_bytes("big.bin", b"z" * 1000)

        stored = await att.offload(blobs)

        assert stored.is_offloaded
        assert stored.ref.startswith("blob:")
        assert stored.size == 1000  # size survives the move
        assert await stored.read(blobs) == b"z" * 1000
        # The original still holds its bytes.
        assert att.data == b"z" * 1000

    async def test_reading_an_offloaded_attachment_without_blobs_raises(
        self, blobs: BlobService
    ) -> None:
        """Better than silently returning empty bytes and writing a 0-byte file."""
        stored = await Attachment.from_bytes("x.bin", b"data").offload(blobs)

        with pytest.raises(ValueError, match="no blob service"):
            await stored.read()

    async def test_attachment_flows_through_a_workflow(self) -> None:
        @step
        async def render() -> Attachment:
            """Produce a file."""
            return Attachment.from_text("invoice.txt", "total: 42")

        @step
        async def summarise(att: Attachment) -> str:
            """Consume it, using the metadata that came with it."""
            return f"{att.filename} ({att.mime}) {att.size}B"

        @workflow(name="attachment_flow")
        async def flow(ctx: Context, _input: str) -> str:
            att = await ctx.step(render)
            return await ctx.step(summarise, att)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(flow, "go")

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "invoice.txt (text/plain) 9B"

    async def test_attachment_survives_replay(self) -> None:
        @step
        async def render() -> Attachment:
            """Produce a file."""
            return Attachment.from_bytes("x.bin", bytes(range(64)))

        @workflow(name="attachment_replay")
        async def flow(ctx: Context, _input: str) -> int:
            att = await ctx.step(render)
            return len(att.data)

        rt = Runtime(store=MemoryStore())
        first = await rt.run(flow, "go")
        replayed = await rt.replay(first.run_id)

        assert replayed.status is ExecutionStatus.COMPLETED
        assert replayed.output == 64


# ---------------------------------------------------------------------------
# Versioned artifacts
# ---------------------------------------------------------------------------


class TestArtifactService:
    async def test_first_publish_is_version_one(self, artifacts: ArtifactService) -> None:
        version = await artifacts.put("report.md", b"v1")

        assert version.version == 1
        assert version.qualified_name == "report.md@1"
        assert version.size == 2

    async def test_changed_content_makes_a_new_version(
        self, artifacts: ArtifactService
    ) -> None:
        await artifacts.put("report.md", b"v1")
        second = await artifacts.put("report.md", b"v2")

        assert second.version == 2
        assert await artifacts.read("report.md") == b"v2"
        assert await artifacts.read("report.md", 1) == b"v1"

    async def test_identical_content_does_not_make_a_version(
        self, artifacts: ArtifactService
    ) -> None:
        """A retry or replay republishing the same bytes must not bump the version."""
        first = await artifacts.put("report.md", b"same")
        again = await artifacts.put("report.md", b"same")

        assert again.version == first.version == 1
        assert len(await artifacts.history("report.md")) == 1

    async def test_content_can_return_to_an_earlier_value(
        self, artifacts: ArtifactService
    ) -> None:
        await artifacts.put("cfg.json", b"a")
        await artifacts.put("cfg.json", b"b")
        third = await artifacts.put("cfg.json", b"a")

        # Only consecutive duplicates collapse; a real reversion is a new version.
        assert third.version == 3

    async def test_names_are_independent(self, artifacts: ArtifactService) -> None:
        await artifacts.put("a.txt", b"1")
        b = await artifacts.put("b.txt", b"2")

        assert b.version == 1

    async def test_unknown_name_raises(self, artifacts: ArtifactService) -> None:
        with pytest.raises(ArtifactNotFound, match=r"missing\.txt"):
            await artifacts.get("missing.txt")

    async def test_unknown_version_says_what_the_latest_is(
        self, artifacts: ArtifactService
    ) -> None:
        await artifacts.put("r.md", b"v1")

        with pytest.raises(ArtifactNotFound, match="latest is 1"):
            await artifacts.get("r.md", 9)

    async def test_run_provenance_is_recorded(self, artifacts: ArtifactService) -> None:
        version = await artifacts.put("r.md", b"x", run_id="run-77")
        assert version.created_by_run == "run-77"

    async def test_store_backed_index_persists(self, blobs: BlobService) -> None:
        """The index survives rebuilding the service, since it lives in the store."""
        store = MemoryStore()

        first = ArtifactService(blobs, StoreBackedArtifactStore(store))
        await first.put("r.md", b"v1")
        await first.put("r.md", b"v2")

        rebuilt = ArtifactService(blobs, StoreBackedArtifactStore(store))
        assert len(await rebuilt.history("r.md")) == 2
        assert await rebuilt.read("r.md") == b"v2"


class TestArtifactContextApi:
    @pytest.fixture
    def runtime(self, blobs: BlobService) -> Runtime:
        return Runtime(store=MemoryStore(), blobs=blobs)

    async def test_put_and_get_through_ctx(self, runtime: Runtime) -> None:
        @workflow(name="artifact_flow")
        async def flow(ctx: Context, _input: str) -> str:
            await ctx.put_artifact("out.txt", b"hello", mime="text/plain")
            return (await ctx.get_artifact("out.txt")).decode()

        result = await runtime.run(flow, "go")

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "hello"

    async def test_versions_accumulate_across_runs(self, runtime: Runtime) -> None:
        @workflow(name="artifact_versions")
        async def flow(ctx: Context, body: str) -> int:
            version = await ctx.put_artifact("doc.txt", body.encode())
            return version.version

        assert (await runtime.run(flow, "one")).output == 1
        assert (await runtime.run(flow, "two")).output == 2

        history = await runtime.artifacts.history("doc.txt")
        assert [v.version for v in history] == [1, 2]
        # Each version knows which run made it.
        assert len({v.created_by_run for v in history}) == 2

    async def test_replay_pins_the_version_it_originally_read(
        self, runtime: Runtime
    ) -> None:
        """A replay is a rehearsal of what happened, not of what would happen now."""

        @workflow(name="artifact_pinned")
        async def reader(ctx: Context, _input: str) -> str:
            return (await ctx.get_artifact("shared.txt")).decode()

        await runtime.artifacts.put("shared.txt", b"original")
        first = await runtime.run(reader, "go")
        assert first.output == "original"

        # Someone publishes a newer version after the run.
        await runtime.artifacts.put("shared.txt", b"updated")

        replayed = await runtime.replay(first.run_id)
        assert replayed.output == "original"

    async def test_artifact_history_through_ctx(self, runtime: Runtime) -> None:
        @workflow(name="artifact_history")
        async def flow(ctx: Context, _input: str) -> int:
            await ctx.put_artifact("h.txt", b"a")
            await ctx.put_artifact("h.txt", b"b")
            return len(await ctx.artifact_versions("h.txt"))

        assert (await runtime.run(flow, "go")).output == 2

    async def test_artifacts_without_blobs_gives_a_useful_error(self) -> None:
        @workflow(name="artifact_unconfigured")
        async def flow(ctx: Context, _input: str) -> str:
            await ctx.put_artifact("x.txt", b"data")
            return "unreachable"

        result = await Runtime(store=MemoryStore()).run(flow, "go")

        assert result.status is ExecutionStatus.FAILED
        assert "blobs=BlobService" in result.error.message


class TestRetentionDeletesBlobs:
    async def test_compaction_reclaims_blob_storage(self, blobs: BlobService) -> None:
        """Compacting runs while leaving their blobs behind leaks storage forever."""
        from loom.blobs.retention import RetentionManager, RetentionPolicy

        big = "x" * (400 * 1024)

        @step
        async def produce() -> str:
            """Return something large enough to be offloaded."""
            return big

        @workflow(name="retention_blob_flow")
        async def flow(ctx: Context, _input: str) -> int:
            return len(await ctx.step(produce))

        store = MemoryStore()
        rt = Runtime(store=store, blobs=blobs)
        result = await rt.run(flow, "go")

        entry = next(
            e for e in await store.load_journal(result.run_id) if e.name == "produce"
        )
        ref = entry.output["__blob__"]
        assert await blobs.load(ref)

        # Age the run past the record cutoff.
        record = await store.get_execution(result.run_id)
        record.finished_at = datetime.now(UTC) - timedelta(days=400)
        await store.update_execution(record)

        report = await RetentionManager(RetentionPolicy()).compact(store, blobs=blobs)

        assert report.runs_archived == 1
        assert report.payloads_deleted == 1
        from loom.blobs.blob import BlobNotFoundError

        with pytest.raises(BlobNotFoundError):
            await blobs.load(ref)

    async def test_compaction_without_blobs_leaves_them_alone(
        self, blobs: BlobService
    ) -> None:
        from loom.blobs.retention import RetentionManager, RetentionPolicy

        store = MemoryStore()
        report = await RetentionManager(RetentionPolicy()).compact(store)
        assert report.payloads_deleted == 0


# ---------------------------------------------------------------------------
# Orphan recovery
# ---------------------------------------------------------------------------


@step
async def slow_step(n: int) -> int:
    """A step that just returns."""
    return n


@workflow(name="orphan_flow")
async def orphan_flow(ctx: Context, n: int) -> int:
    """One durable step."""
    return await ctx.step(slow_step, n)


class TestOrphanRecovery:
    async def test_running_record_with_a_live_lease_is_left_alone(self) -> None:
        from loom.core.models import ExecutionRecord

        store = MemoryStore()
        await store.create_execution(
            ExecutionRecord(
                run_id="live",
                workflow="orphan_flow",
                status=ExecutionStatus.RUNNING,
                lease_owner="other-node",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

        rt = Runtime(store=store)
        assert await rt.reclaim_orphans() == []

    async def test_expired_lease_is_reclaimed_and_finished(self) -> None:
        """The crashed worker's run resumes and completes on another node."""
        from loom.core.models import ExecutionRecord
        from loom.core.serde import encode

        store = MemoryStore()
        await store.create_execution(
            ExecutionRecord(
                run_id="orphan",
                workflow="orphan_flow",
                status=ExecutionStatus.RUNNING,
                input=encode(7),
                lease_owner="dead-node",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

        rt = Runtime(store=store)
        rt.register(orphan_flow)
        try:
            assert await rt.reclaim_orphans() == ["orphan"]
            final = await rt.wait("orphan", timeout=5)
            assert final.status is ExecutionStatus.COMPLETED
            assert final.output == 7
        finally:
            await rt.shutdown()

    async def test_a_record_without_a_lease_is_not_an_orphan(self) -> None:
        from loom.core.models import ExecutionRecord

        store = MemoryStore()
        await store.create_execution(
            ExecutionRecord(
                run_id="no-lease",
                workflow="orphan_flow",
                status=ExecutionStatus.RUNNING,
            )
        )

        assert await Runtime(store=store).reclaim_orphans() == []

    async def test_an_orphan_is_found_behind_a_page_of_healthy_runs(self) -> None:
        """The starvation case, and the reason `limit` bounds results not reads.

        The lease lives inside the record's JSON, so it is filtered in Python
        after the store query — and that query orders newest-first, `run_id`
        being a ULID. An orphan is a run that stopped advancing, so it carries
        one of the *oldest* ids. Behind a page of healthy runs it was never in
        the window that got filtered, and `reclaim_orphans` returned `[]`: the
        precise set it exists to rescue, invisible to it, reported as "nothing
        to do".

        150 healthy runs here against the old single page of 100.
        """
        from loom.core.models import ExecutionRecord
        from loom.core.serde import encode

        store = MemoryStore()
        # Oldest id, so every healthy run sorts ahead of it.
        await store.create_execution(
            ExecutionRecord(
                run_id="00000000-orphan",
                workflow="orphan_flow",
                status=ExecutionStatus.RUNNING,
                input=encode(7),
                lease_owner="dead-node",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        for i in range(150):
            await store.create_execution(
                ExecutionRecord(
                    run_id=f"zz{i:04d}-healthy",
                    workflow="orphan_flow",
                    status=ExecutionStatus.RUNNING,
                    lease_owner="live-node",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )

        rt = Runtime(store=store)
        rt.register(orphan_flow)
        try:
            assert await rt.reclaim_orphans() == ["00000000-orphan"]
        finally:
            await rt.shutdown()

    async def test_the_scan_ceiling_is_reported_not_silent(self, caplog) -> None:
        """A bounded scan that gives up must say so.

        Stopping quietly at a ceiling is the same defect as the single page,
        one page bigger — it reads as "no orphans" either way.

        Exercises the *fallback* path deliberately. The shipped stores answer
        this from an indexed column and never scan, so the ceiling only governs
        a host's own `ExecutionStore` that does not implement `due_leases` —
        which is exactly the store that would otherwise regress unnoticed.
        """
        import logging

        from loom.core.models import ExecutionRecord

        class NoIndexedScan(MemoryStore):
            """A store predating `due_leases`, as a host's own might be."""

            due_leases = None

        store = NoIndexedScan()
        for i in range(60):
            await store.create_execution(
                ExecutionRecord(
                    run_id=f"r{i:04d}",
                    workflow="orphan_flow",
                    status=ExecutionStatus.RUNNING,
                    lease_owner="live-node",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )

        rt = Runtime(store=store)
        with caplog.at_level(logging.WARNING):
            assert await rt.reclaim_orphans(scan_limit=10) == []

        assert any("scan_limit" in r.message for r in caplog.records)

    async def test_a_run_this_node_is_driving_is_not_reclaimed(self) -> None:
        rt = Runtime(store=MemoryStore())
        rt._driving.add("mine")
        from loom.core.models import ExecutionRecord

        await rt.store.create_execution(
            ExecutionRecord(
                run_id="mine",
                workflow="orphan_flow",
                status=ExecutionStatus.RUNNING,
                lease_owner=rt.node_id,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

        assert await rt.reclaim_orphans() == []

    async def test_lease_is_taken_while_running_and_released_after(self) -> None:
        store = MemoryStore()
        seen: dict[str, object] = {}

        @step
        async def observe(run_id: str) -> str:
            """Read our own record mid-flight, while the lease should be held."""
            record = await store.get_execution(run_id)
            seen["owner"] = record.lease_owner
            seen["expires"] = record.lease_expires_at
            return run_id

        @workflow(name="lease_flow")
        async def flow(ctx: Context, _input: str) -> str:
            return await ctx.step(observe, ctx.run_id)

        rt = Runtime(store=store)
        result = await rt.run(flow, "go")

        assert seen["owner"] == rt.node_id
        assert seen["expires"] is not None

        # Released once terminal, so it is never mistaken for an orphan.
        record = await rt.get(result.run_id)
        assert record.lease_owner is None
        assert record.lease_expires_at is None

    async def test_node_id_is_stable_and_distinct(self) -> None:
        a, b = Runtime(store=MemoryStore()), Runtime(store=MemoryStore())
        assert a.node_id != b.node_id
        assert Runtime(store=MemoryStore(), node_id="fixed").node_id == "fixed"


# ---------------------------------------------------------------------------
# Smoke running generated code
# ---------------------------------------------------------------------------


_GOOD_FLOW = '''
from loom import Context, step, workflow


@step
async def double(n: int) -> int:
    """Double it."""
    return n * 2


@workflow(name="generated")
async def generated(ctx: Context, n: int) -> int:
    """Double the input."""
    return await ctx.step(double, n or 1)
'''


class TestSmokeRun:
    def test_compile_check_catches_what_ast_parse_allows(self) -> None:
        from loom.agents.smoke import compile_check

        # `return` outside a function parses but does not compile.
        assert not compile_check("return 5").ok
        assert compile_check(_GOOD_FLOW).ok

    def test_good_code_runs(self) -> None:
        from loom.agents.smoke import smoke_run

        result = smoke_run(_GOOD_FLOW, 21)

        assert result.ok, result.error
        assert result.phase == "done"
        assert result.status == "completed"
        assert result.output_preview == "42"
        assert result.workflows_found == ["generated"]

    def test_import_error_is_reported_not_raised(self) -> None:
        from loom.agents.smoke import smoke_run

        code = "import definitely_not_a_real_package_xyz\n" + _GOOD_FLOW
        result = smoke_run(code)

        assert not result.ok
        assert result.phase == "import"
        assert "definitely_not_a_real_package_xyz" in result.error

    def test_a_file_with_no_workflow_fails(self) -> None:
        from loom.agents.smoke import smoke_run

        result = smoke_run("x = 1\n")

        assert not result.ok
        assert "no @workflow" in result.error

    def test_a_failing_workflow_is_not_reported_as_passing(self) -> None:
        from loom.agents.smoke import smoke_run

        code = '''
from loom import Context, step, workflow


@step(retry=1)
async def boom() -> int:
    """Always fails."""
    raise ValueError("bad configuration")


@workflow(name="broken")
async def broken(ctx: Context, _n: int) -> int:
    return await ctx.step(boom)
'''
        result = smoke_run(code)

        assert not result.ok
        assert result.phase == "run"
        assert "bad configuration" in result.error

    def test_an_infinite_loop_times_out(self) -> None:
        from loom.agents.smoke import smoke_run

        code = '''
from loom import Context, step, workflow


@step
async def spin() -> int:
    """Never returns."""
    while True:
        pass


@workflow(name="hangs")
async def hangs(ctx: Context, _n: int) -> int:
    return await ctx.step(spin)
'''
        result = smoke_run(code, timeout=2.0)

        assert not result.ok
        assert "did not finish" in result.error

    def test_feedback_is_phrased_as_a_repair_instruction(self) -> None:
        from loom.agents.smoke import smoke_run

        result = smoke_run("import nope_not_real_pkg\n" + _GOOD_FLOW)
        feedback = result.as_feedback()

        assert "failed during import" in feedback
        assert "corrected file" in feedback.lower()

    def test_feedback_carries_the_code_that_failed(self) -> None:
        """The coding agent is ephemeral, so a repair round that sends only the
        traceback asks the model to fix code it cannot see."""
        from loom.agents.smoke import smoke_run

        broken = "import nope_not_real_pkg\n" + _GOOD_FLOW
        feedback = smoke_run(broken).as_feedback(broken)

        assert "nope_not_real_pkg" in feedback
        assert "async def double" in feedback  # the actual source is included

    def test_agent_calls_resolve_against_a_mock_model(self) -> None:
        """No API key, no network — the mock provider stands in."""
        from loom.agents.smoke import smoke_run

        code = '''
from loom import Context, step, workflow


@step
async def tidy(text: str) -> str:
    """Trim the reply."""
    return text.strip()


@workflow(name="uses_agent")
async def uses_agent(ctx: Context, _n: int) -> str:
    reply = await ctx.agent("summarise something")
    return await ctx.step(tidy, reply.output)
'''
        result = smoke_run(code)

        assert result.ok, result.error
        assert "mock agent reply" in result.output_preview


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _loom(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI the way a user does — through its module entry point."""
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


class TestCli:
    def test_the_console_entry_point_imports(self) -> None:
        """The shipped script used to fail with ImportError before it did anything."""
        from loom.cli import main

        assert callable(main)

    def test_version(self) -> None:
        from loom import __version__

        done = _loom("--version")
        assert done.returncode == 0
        assert __version__ in done.stdout

    def test_bare_invocation_prints_help(self) -> None:
        done = _loom()
        assert done.returncode == 0
        assert "check" in done.stdout and "init" in done.stdout

    def test_missing_file_is_an_error_not_a_traceback(self) -> None:
        done = _loom("check", "no_such_file.py")

        assert done.returncode == 2
        assert "no such file" in done.stderr
        assert "Traceback" not in done.stderr

    def test_init_scaffolds_a_working_project(self, tmp_path: Path) -> None:
        done = _loom("init", str(tmp_path / "proj"))
        assert done.returncode == 0

        project = tmp_path / "proj"
        assert (project / "workflows" / "quickstart.py").exists()
        assert (project / "tests" / "test_quickstart.py").exists()

        # The scaffolded workflow actually runs.
        ran = subprocess.run(
            [sys.executable, "workflows/quickstart.py"],
            capture_output=True,
            text=True,
            cwd=project,
        )
        assert ran.returncode == 0, ran.stderr
        assert "completed" in ran.stdout

    def test_init_is_additive_not_destructive(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        (project / "workflows").mkdir(parents=True)
        (project / "workflows" / "quickstart.py").write_text("# mine\n")

        _loom("init", str(project))

        assert (project / "workflows" / "quickstart.py").read_text() == "# mine\n"

    def test_init_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        done = _loom("init", str(tmp_path / "proj"), "--dry-run")

        assert done.returncode == 0
        assert "quickstart.py" in done.stdout
        assert not (tmp_path / "proj").exists()


# ---------------------------------------------------------------------------
# Visualization pipeline
# ---------------------------------------------------------------------------


_FLOW_SOURCE = '''
"""A flow with two steps and a branch."""

from loom import Context, step, workflow


@step
async def fetch(key: str) -> dict:
    """Fetch a record."""
    return {"key": key}


@step
async def enrich(record: dict) -> dict:
    """Add a field."""
    return {**record, "enriched": True}


@workflow(name="demo_flow")
async def demo_flow(ctx: Context, key: str) -> dict:
    """Fetch and conditionally enrich."""
    record = await ctx.step(fetch, key)
    if key:
        record = await ctx.step(enrich, record)
    return record


def helper_not_in_the_graph() -> int:
    """A module-level helper. Must not appear as a node."""
    if True:
        return 1
    return 0


if __name__ == "__main__":
    helper_not_in_the_graph()
'''


@pytest.fixture
def flow_file(tmp_path: Path) -> Path:
    target = tmp_path / "demo_flow.py"
    target.write_text(_FLOW_SOURCE)
    return target


class TestGraphPipeline:
    def test_extraction_is_scoped_to_the_workflow_body(self, flow_file: Path) -> None:
        """Module-level code and helpers must not become graph nodes."""
        from loom.graph.pipeline import build_graph

        graph = build_graph(flow_file)
        labels = [n.label for n in graph.nodes]

        assert "fetch" in labels
        assert "enrich" in labels
        # Exactly one branch — the helper's `if True` is not part of the flow.
        assert labels.count("if") == 1

    def test_registry_pass_supplies_docstrings(self, flow_file: Path) -> None:
        from loom.graph.pipeline import build_graph

        graph = build_graph(flow_file)
        fetch = graph.find_node("fetch")

        assert fetch is not None
        assert fetch.description == "Fetch a record."

    def test_check_writes_both_artifacts(self, flow_file: Path) -> None:
        from loom.graph.pipeline import check_file

        report = check_file(flow_file)

        graph_path = flow_file.with_suffix(".graph.json")
        description_path = flow_file.with_suffix(".description.md")
        assert graph_path.exists() and description_path.exists()
        assert set(report.written) == {graph_path, description_path}
        assert report.problems == []

    def test_check_is_idempotent(self, flow_file: Path) -> None:
        """Re-running with no source change must not produce a noisy diff."""
        from loom.graph.pipeline import check_file

        check_file(flow_file)
        second = check_file(flow_file)

        assert second.written == []
        assert not second.graph_changed

    def test_check_detects_a_structural_change(self, flow_file: Path) -> None:
        from loom.graph.pipeline import check_file

        check_file(flow_file)
        flow_file.write_text(
            _FLOW_SOURCE.replace(
                "    return record",
                "    record = await ctx.step(enrich, record)\n    return record",
            )
        )

        assert check_file(flow_file).graph_changed

    def test_no_write_leaves_the_disk_alone(self, flow_file: Path) -> None:
        from loom.graph.pipeline import check_file

        check_file(flow_file, write=False)

        assert not flow_file.with_suffix(".graph.json").exists()

    def test_narration_covers_every_node(self, flow_file: Path) -> None:
        """The completeness check is what stops a model hiding a step."""
        import asyncio

        from loom.graph.explainer import (
            SkeletonExplainer,
            verify_completeness,
        )
        from loom.graph.pipeline import build_graph

        graph = build_graph(flow_file)
        narration = asyncio.run(SkeletonExplainer().narrate(graph))

        assert verify_completeness(narration, graph) == []

    def test_an_incomplete_narration_is_reported(self, flow_file: Path) -> None:
        from loom.graph.explainer import Narration
        from loom.graph.pipeline import check_file

        class ForgetfulExplainer:
            async def narrate(self, graph):
                return Narration(summary="partial", node_descriptions={}, full_text="x")

        report = check_file(flow_file, explainer=ForgetfulExplainer())

        assert report.problems
        assert "omitted" in report.problems[0]

    def test_unimportable_file_degrades_to_the_ast_pass(self, tmp_path: Path) -> None:
        """A missing dependency should still yield a usable skeleton."""
        from loom.graph.pipeline import build_graph

        broken = tmp_path / "broken.py"
        broken.write_text(
            "import definitely_missing_pkg_xyz\n" + _FLOW_SOURCE
        )

        graph = build_graph(broken)
        assert [n.label for n in graph.nodes]  # AST pass still found the ctx calls


class TestReactFlowExport:
    def test_shape_matches_react_flow(self, flow_file: Path) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.reactflow import to_react_flow

        payload = to_react_flow(build_graph(flow_file))

        assert payload["flowId"] == "demo_flow"
        for node in payload["nodes"]:
            assert {"id", "type", "position", "data"} <= set(node)
            assert {"x", "y"} == set(node["position"])
        for edge in payload["edges"]:
            assert {"id", "source", "target"} <= set(edge)

    def test_supplied_positions_win(self, flow_file: Path) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.reactflow import to_react_flow

        graph = build_graph(flow_file)
        payload = to_react_flow(graph, positions={"fetch": (11.0, 22.0)})

        fetch = next(n for n in payload["nodes"] if n["id"] == "fetch")
        assert fetch["position"] == {"x": 11.0, "y": 22.0}

    def test_fallback_positions_are_assigned(self, flow_file: Path) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.reactflow import to_react_flow

        payload = to_react_flow(build_graph(flow_file))
        xs = {node["position"]["x"] for node in payload["nodes"]}

        # Nodes are spread across columns rather than stacked at the origin.
        assert len(xs) > 1

    def test_edge_ids_are_unique(self, flow_file: Path) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.reactflow import to_react_flow

        edges = to_react_flow(build_graph(flow_file))["edges"]
        assert len({e["id"] for e in edges}) == len(edges)

    async def test_run_status_overlays_onto_nodes(self, flow_file: Path) -> None:
        """The same canvas renders a live run, not just the static shape."""
        from loom.graph.reactflow import to_react_flow
        from loom.graph.trace import NodeTrace, RunTrace
        from loom.graph.wgir import NodeKind, WGIRGraph, WGIRNode

        graph = WGIRGraph(
            flow_id="f",
            nodes=[
                WGIRNode(id="fetch", kind=NodeKind.EFFECT, label="fetch"),
                WGIRNode(id="save", kind=NodeKind.EFFECT, label="save"),
            ],
        )
        trace = RunTrace(
            run_id="r",
            flow_id="f",
            node_traces={"fetch": NodeTrace(node_id="fetch", status="completed")},
        )

        payload = to_react_flow(graph, trace=trace)
        by_id = {node["id"]: node for node in payload["nodes"]}

        assert by_id["fetch"]["data"]["status"] == "completed"
        # A node the run never reached has no status rather than a wrong one.
        assert by_id["save"]["data"]["status"] is None
