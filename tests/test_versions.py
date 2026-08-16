"""Versions as a Runtime capability, on top of the port's own conformance suite.

``tests/conformance/test_version_store.py`` proves the port behaves identically
on every store. This proves the Runtime wires it correctly and, crucially, that
a Runtime which never asks for versions pays nothing — the property that keeps
an opt-in feature from becoming a tax on everyone else.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom import Context, Runtime, workflow
from loom.runtime.versions import (
    MissingVersionContent,
    Pins,
    StoreBackedVersionStore,
    VersionStore,
    WorkflowVersion,
)
from loom.stores.memory import MemoryStore

SOURCE = "async def onboard(ctx, email): return email.upper()"
EDITED = "async def onboard(ctx, email): return email.lower()"


@workflow(name="onboard")
async def onboard(ctx: Context, email: str) -> str:
    return email.upper()


@pytest.fixture
def runtime() -> Runtime:
    made = Runtime(store=MemoryStore())
    made.register(onboard)
    return made


class TestPublishingWithSource:
    async def test_publish_without_source_writes_no_version(
        self, runtime: Runtime
    ) -> None:
        """The default. A host that keeps the file as its source of truth must
        not silently acquire a second copy of every workflow."""
        await runtime.publish(onboard)
        assert await runtime.versions.history("onboard") == []
        assert await runtime.versions.latest("onboard") is None

    async def test_publish_with_source_commits_one(self, runtime: Runtime) -> None:
        record = await runtime.publish(onboard, source=SOURCE)
        assert record.metadata["version_number"] == 1

        latest = await runtime.versions.latest("onboard")
        assert latest is not None and latest.version == 1
        assert await runtime.versions.source_of(latest) == SOURCE

    async def test_republishing_unchanged_source_does_not_add_a_version(
        self, runtime: Runtime
    ) -> None:
        await runtime.publish(onboard, source=SOURCE)
        await runtime.publish(onboard, source=SOURCE)
        assert len(await runtime.versions.history("onboard")) == 1

    async def test_pins_reach_the_version(self, runtime: Runtime) -> None:
        await runtime.publish(
            onboard, source=SOURCE, pins=Pins(toolsets={"jira": "1.2"})
        )
        latest = await runtime.versions.latest("onboard")
        assert latest.pins.toolsets == {"jira": "1.2"}


class TestTyingARunToItsCode:
    async def test_version_of_finds_the_code_that_ran(self, runtime: Runtime) -> None:
        """The question the catalog alone could not answer.

        ``code_hash`` and ``content_hash`` are different fingerprints — one of
        the function body, one of the source text — and a first cut recorded
        only the second, so every run resolved to no version at all.
        """
        await runtime.publish(onboard, source=SOURCE)
        result = await runtime.run(onboard, "a@b.com")

        version = await runtime.version_of(result.run_id)
        assert version is not None
        assert version.version == 1
        assert await runtime.versions.source_of(version) == SOURCE

    async def test_version_of_is_none_when_nothing_was_versioned(
        self, runtime: Runtime
    ) -> None:
        result = await runtime.run(onboard, "a@b.com")
        assert await runtime.version_of(result.run_id) is None

    async def test_version_of_an_unknown_run_is_none(self, runtime: Runtime) -> None:
        assert await runtime.version_of("run_nope") is None

    async def test_a_run_resolves_to_the_version_it_ran_not_the_latest(
        self, runtime: Runtime
    ) -> None:
        """The whole point of recording the hash.

        A newer version must not rewrite what an old run is understood to have
        executed.
        """
        await runtime.publish(onboard, source=SOURCE)
        result = await runtime.run(onboard, "a@b.com")

        # A later commit of different source, same workflow.
        await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED, code_hash="different")
        )
        assert (await runtime.versions.latest("onboard")).version == 2

        version = await runtime.version_of(result.run_id)
        assert version.version == 1, "the run resolved to code it never executed"


class TestItIsOptional:
    def test_a_bare_runtime_has_a_version_store_and_writes_nothing(self) -> None:
        made = Runtime(store=MemoryStore())
        assert isinstance(made.versions, VersionStore)

    async def test_a_host_can_supply_its_own(self) -> None:
        """The seam PipesHub (or anyone) plugs a graph database into."""
        calls: list[str] = []

        class HostVersions:
            async def commit(self, version, *, expected_latest=None):
                calls.append("commit")
                return version.model_copy(update={"version": 7})

            async def get(self, workflow, version):
                return None

            async def latest(self, workflow):
                calls.append("latest")
                return None

            async def history(self, workflow, *, limit=50):
                return []

            async def resolve(self, workflow, digest):
                return None

            async def source_of(self, version):
                return "from the host"

        made = Runtime(store=MemoryStore(), versions=HostVersions())
        made.register(onboard)
        record = await made.publish(onboard, source=SOURCE)

        assert calls == ["commit"]
        assert record.metadata["version_number"] == 7

    async def test_source_that_is_not_retrievable_raises_rather_than_guessing(
        self,
    ) -> None:
        """Falling back to the file on disk would make a replay rehearse
        something that never happened."""
        versions = StoreBackedVersionStore(MemoryStore())
        orphan = WorkflowVersion(workflow="onboard", version=1, source_ref="store:gone")
        with pytest.raises(MissingVersionContent, match="not retrievable"):
            await versions.source_of(orphan)

    async def test_a_version_with_no_ref_at_all_raises(self) -> None:
        versions = StoreBackedVersionStore(MemoryStore())
        with pytest.raises(MissingVersionContent, match="records no source_ref"):
            await versions.source_of(WorkflowVersion(workflow="onboard", version=1))


class TestBlobBackedContent:
    async def test_source_goes_to_blobs_when_one_is_configured(
        self, tmp_path: Any
    ) -> None:
        """R4: records hold references, blobs hold content — so a large
        workflow is not a document-store problem."""
        from loom.blobs.blob import BlobService, LocalBlobBackend

        blobs = BlobService(LocalBlobBackend(tmp_path / "blobs"))
        made = Runtime(store=MemoryStore(), blobs=blobs)
        made.register(onboard)

        await made.publish(onboard, source=SOURCE)
        latest = await made.versions.latest("onboard")

        assert latest.source_ref.startswith("blob:"), latest.source_ref
        assert await made.versions.source_of(latest) == SOURCE
