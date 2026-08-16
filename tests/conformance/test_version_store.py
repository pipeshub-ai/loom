"""``VersionStore`` against every backend, and against blob-backed content.

The new port joins the existing matrix rather than getting its own tests on one
store, because the whole point of P0 was that per-store tests are how four
backends end up four subtly different products.

Two axes here, not one: every *store*, and content stored inline versus in a
blob service. A version that round-trips on Memory-with-no-blobs and fails on
Postgres-with-S3 is the same failure class as the ones the store matrix found.
"""

from __future__ import annotations

from typing import Any

import pytest

from conformance.backends import ALL_BACKENDS, open_store
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.runtime.versions import (
    Pins,
    StoreBackedVersionStore,
    VersionConflict,
    WorkflowVersion,
    content_hash_of,
)

SOURCE = '''
from loom import Context, workflow


@workflow(name="onboard")
async def onboard(ctx: Context, email: str) -> str:
    return email.upper()
'''

EDITED = SOURCE.replace("upper", "lower")


@pytest.fixture(params=[backend.name for backend in ALL_BACKENDS])
async def store(request):
    async with open_store(request.param) as made:
        yield made


@pytest.fixture(params=["inline", "blobs"])
def versions(request, store, tmp_path) -> Any:
    """Both content strategies, so neither is only exercised by accident."""
    if request.param == "inline":
        return StoreBackedVersionStore(store)
    blobs = BlobService(LocalBlobBackend(tmp_path / "blobs"))
    return StoreBackedVersionStore(store, blobs)


def _version(source: str = SOURCE, **overrides: Any) -> WorkflowVersion:
    return WorkflowVersion(workflow="onboard", source=source, **overrides)


class TestCommitAndRead:
    async def test_a_commit_gets_the_first_number(self, versions) -> None:
        committed = await versions.commit(_version())
        assert committed.version == 1
        assert committed.parent_version is None
        assert committed.content_hash == content_hash_of(SOURCE)

    async def test_numbers_are_monotonic_and_linked(self, versions) -> None:
        first = await versions.commit(_version())
        second = await versions.commit(_version(EDITED))
        assert (first.version, second.version) == (1, 2)
        assert second.parent_version == 1

    async def test_the_source_round_trips(self, versions) -> None:
        """The whole reason versions exist: get the code back, not its hash."""
        committed = await versions.commit(_version())
        assert await versions.source_of(committed) == SOURCE

    async def test_a_large_source_round_trips(self, versions) -> None:
        """Records stay small because content goes elsewhere — the property
        that keeps a big workflow from being a document-store problem."""
        big = SOURCE + "\n# " + ("x" * 200_000)
        committed = await versions.commit(_version(big))
        assert await versions.source_of(committed) == big
        assert len(committed.model_dump_json()) < 4_000, (
            "the record grew with the source — content is not being offloaded"
        )

    async def test_get_and_latest_agree(self, versions) -> None:
        await versions.commit(_version())
        second = await versions.commit(_version(EDITED))
        assert (await versions.latest("onboard")).version == second.version
        assert (await versions.get("onboard", 1)).version == 1

    async def test_history_is_newest_first(self, versions) -> None:
        await versions.commit(_version())
        await versions.commit(_version(EDITED))
        assert [v.version for v in await versions.history("onboard")] == [2, 1]

    async def test_unknown_reads_are_none_not_errors(self, versions) -> None:
        assert await versions.get("onboard", 99) is None
        assert await versions.latest("never-published") is None
        assert await versions.history("never-published") == []

    async def test_pins_and_metadata_survive(self, versions) -> None:
        committed = await versions.commit(
            _version(
                pins=Pins(toolsets={"jira": "1.2"}, nodes={"human.approval": "1.0.0"}),
                verifier_version="2026.8",
                created_by="dana",
                message="first cut",
            )
        )
        read = await versions.get("onboard", committed.version)
        assert read.pins.toolsets == {"jira": "1.2"}
        assert read.pins.nodes == {"human.approval": "1.0.0"}
        assert read.verifier_version == "2026.8"
        assert read.created_by == "dana" and read.message == "first cut"

    async def test_workflows_do_not_share_a_sequence(self, versions) -> None:
        await versions.commit(_version())
        other = await versions.commit(
            WorkflowVersion(workflow="other", source="x = 1")
        )
        assert other.version == 1, "version numbers leaked across workflows"


class TestIdentity:
    async def test_identical_source_returns_the_existing_version(
        self, versions
    ) -> None:
        """A retried publish must not inflate the chain — the property
        artifacts already have."""
        first = await versions.commit(_version())
        again = await versions.commit(_version())
        assert again.version == first.version
        assert len(await versions.history("onboard")) == 1

    async def test_resolve_ties_a_hash_back_to_a_version(self, versions) -> None:
        """How a run finds the code that produced it: ExecutionRecord carries a
        code_hash, and this turns it into a version."""
        committed = await versions.commit(_version())
        found = await versions.resolve("onboard", committed.content_hash)
        assert found is not None and found.version == committed.version

    async def test_resolve_misses_are_none(self, versions) -> None:
        await versions.commit(_version())
        assert await versions.resolve("onboard", "deadbeef") is None
        assert await versions.resolve("onboard", "") is None


class TestConcurrency:
    async def test_a_stale_base_is_refused(self, versions) -> None:
        """Two authors editing one workflow is the normal case, and
        last-write-wins loses work with no trace."""
        await versions.commit(_version())
        await versions.commit(_version(EDITED))

        with pytest.raises(VersionConflict) as caught:
            await versions.commit(_version(SOURCE + "\n# third"), expected_latest=1)
        assert caught.value.expected == 1
        assert caught.value.actual == 2
        assert "somebody else committed" in str(caught.value)

    async def test_a_current_base_is_accepted(self, versions) -> None:
        await versions.commit(_version())
        third = await versions.commit(_version(EDITED), expected_latest=1)
        assert third.version == 2

    async def test_the_first_commit_expects_zero(self, versions) -> None:
        committed = await versions.commit(_version(), expected_latest=0)
        assert committed.version == 1

    async def test_concurrent_commits_do_not_collide_on_a_number(
        self, versions
    ) -> None:
        """Without a number per commit, two versions overwrite each other and
        one author's work disappears."""
        import asyncio

        sources = [SOURCE + f"\n# {n}" for n in range(8)]
        await asyncio.gather(*(versions.commit(_version(s)) for s in sources))

        history = await versions.history("onboard", limit=100)
        numbers = [v.version for v in history]
        assert len(numbers) == len(set(numbers)), f"duplicate numbers: {numbers}"
        assert len(history) == 8, f"commits were lost: kept {len(history)} of 8"


class TestRefusals:
    async def test_a_version_without_source_is_refused(self, versions) -> None:
        with pytest.raises(ValueError, match="needs source"):
            await versions.commit(WorkflowVersion(workflow="onboard"))

    async def test_a_version_without_a_workflow_is_refused(self, versions) -> None:
        with pytest.raises(ValueError, match="name its workflow"):
            await versions.commit(WorkflowVersion(workflow="", source=SOURCE))


class TestTheProtocolIsSatisfied:
    def test_the_default_implements_it(self) -> None:
        from loom.runtime.versions import VersionStore

        assert isinstance(StoreBackedVersionStore(object()), VersionStore)

    def test_a_host_implementation_needs_only_the_protocol(self) -> None:
        """The port must be implementable without importing Loom internals —
        the check that keeps a seam from being decoration."""
        from loom.runtime.versions import VersionStore

        class HostVersions:
            async def commit(self, version, *, expected_latest=None): ...
            async def get(self, workflow, version): ...
            async def latest(self, workflow): ...
            async def history(self, workflow, *, limit=50): ...
            async def resolve(self, workflow, content_hash): ...
            async def source_of(self, version): ...

        assert isinstance(HostVersions(), VersionStore)
