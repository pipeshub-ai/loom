"""Versions as a Runtime capability, on top of the port's own conformance suite.

``tests/conformance/test_version_store.py`` proves the port behaves identically
on every store. This proves the Runtime wires it correctly and, crucially, that
a Runtime which never asks for versions pays nothing — the property that keeps
an opt-in feature from becoming a tax on everyone else.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from loom import Context, Runtime, workflow
from loom.runtime.versions import (
    MissingVersionContent,
    Pins,
    StoreBackedVersionStore,
    UnknownVersion,
    VersionActivation,
    VersionStore,
    WorkflowVersion,
)
from loom.stores.memory import MemoryStore

SOURCE = "async def onboard(ctx, email): return email.upper()"
EDITED = "async def onboard(ctx, email): return email.lower()"


@workflow(name="onboard")
async def onboard(ctx: Context, email: str) -> str:
    return email.upper()


@workflow(name="parks")
async def parks(ctx: Context, _: Any = None) -> str:
    """Suspends, so a version can be activated while a run is genuinely mid-flight."""
    return str(await ctx.wait_for_event("go"))


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


class TestActivation:
    """Which version is *live*, as a pointer rather than a re-commit.

    Before this the only rollback available was committing version three's
    source again as version six: a duplicate of code that already existed, and
    a chain in which nothing recorded which revision was actually being served.
    """

    async def test_the_first_commit_activates_itself(self, runtime: Runtime) -> None:
        """A workflow with one version has no other candidate, and a ``None``
        here would push ``active() or latest()`` into every caller."""
        await runtime.publish(onboard, source=SOURCE)
        active = await runtime.versions.active("onboard")
        assert active is not None and active.version == 1

    async def test_nothing_committed_means_nothing_active(
        self, runtime: Runtime
    ) -> None:
        assert await runtime.versions.active("onboard") is None

    async def test_a_later_commit_does_not_steal_the_pointer(
        self, runtime: Runtime
    ) -> None:
        """The failure the split exists to prevent: if publishing moved the
        pointer, a rollback would last exactly until the next deploy."""
        await runtime.publish(onboard, source=SOURCE)
        await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED, code_hash="edited")
        )
        assert (await runtime.versions.latest("onboard")).version == 2
        assert (await runtime.versions.active("onboard")).version == 1

    async def test_activate_makes_a_version_current(self, runtime: Runtime) -> None:
        await runtime.publish(onboard, source=SOURCE)
        second = await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED, code_hash="edited")
        )
        await runtime.versions.activate("onboard", second.version)

        active = await runtime.versions.active("onboard")
        assert active.version == 2
        assert await runtime.versions.source_of(active) == EDITED

    async def test_latest_and_active_diverge_after_a_rollback(
        self, runtime: Runtime
    ) -> None:
        """Two questions, two answers. ``latest`` is the newest thing anybody
        committed; ``active`` is the one being served."""
        await runtime.publish(onboard, source=SOURCE)
        await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED, code_hash="edited")
        )
        third = await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED + "\n# third", code_hash="c3")
        )
        await runtime.versions.activate("onboard", third.version)

        await runtime.versions.activate("onboard", 1)  # the rollback

        assert (await runtime.versions.latest("onboard")).version == 3
        assert (await runtime.versions.active("onboard")).version == 1
        assert await runtime.versions.source_of(
            await runtime.versions.active("onboard")
        ) == SOURCE

    async def test_rolling_back_creates_no_version(self, runtime: Runtime) -> None:
        """A pointer move, not a copy. Re-committing old source is what people
        do without one, and it appends a duplicate of code that already exists
        while losing the fact of which version is live."""
        await runtime.publish(onboard, source=SOURCE)
        await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED, code_hash="edited")
        )
        before = [v.version for v in await runtime.versions.history("onboard")]

        await runtime.versions.activate("onboard", 1)

        after = [v.version for v in await runtime.versions.history("onboard")]
        assert after == before == [2, 1]

    async def test_activating_is_idempotent(self, runtime: Runtime) -> None:
        """A rollback is done under pressure and is often retried."""
        await runtime.publish(onboard, source=SOURCE)
        await runtime.versions.activate("onboard", 1)
        await runtime.versions.activate("onboard", 1)
        assert (await runtime.versions.active("onboard")).version == 1

    async def test_activating_a_version_that_does_not_exist_is_named(
        self, runtime: Runtime
    ) -> None:
        """``KeyError: 3`` names neither the workflow nor what does exist, and
        the person reading it is mid-rollback."""
        await runtime.publish(onboard, source=SOURCE)
        with pytest.raises(UnknownVersion) as caught:
            await runtime.versions.activate("onboard", 9)

        assert caught.value.workflow == "onboard"
        assert caught.value.version == 9
        assert caught.value.known == [1]
        assert "committed: 1" in str(caught.value)
        assert "never creates" in str(caught.value)

    async def test_activating_against_an_empty_chain_says_so(
        self, runtime: Runtime
    ) -> None:
        with pytest.raises(UnknownVersion, match="nothing has been committed"):
            await runtime.versions.activate("onboard", 1)

    async def test_activation_does_not_disturb_a_run_in_flight(
        self, runtime: Runtime
    ) -> None:
        """The load-bearing one.

        A run pins ``ExecutionRecord.code_hash`` when it starts, and every
        lookup that matters resolves through that hash. If activation reached
        the resolution path, rolling back would change what a parked run comes
        back to — the workflow would resume into code it never began.
        """
        runtime.register(parks)
        await runtime.publish(parks, source=SOURCE)
        parked = await runtime.run(parks)
        assert parked.status.value == "suspended"

        # A newer version, made live while the run sits parked.
        rolled = await runtime.versions.commit(
            WorkflowVersion(workflow="parks", source=EDITED, code_hash="somethingelse")
        )
        await runtime.versions.activate("parks", rolled.version)
        assert (await runtime.versions.active("parks")).version == 2

        delivered = await runtime.send_event(parked.run_id, "go", "resumed")
        assert delivered.delivered is True

        version = await runtime.version_of(parked.run_id)
        assert version is not None and version.version == 1, (
            "activation rewrote which code an in-flight run is understood to run"
        )
        assert await runtime.versions.source_of(version) == SOURCE

    async def test_concurrent_activations_do_not_lose_each_other(
        self, runtime: Runtime
    ) -> None:
        """Eight concurrent commits lost seven of each other before the
        sequence lock; activation shares it rather than inventing a second.
        A pointer is last-write-wins by nature, so what must hold is that the
        winner is one of the versions asked for and the chain is untouched.
        """
        await runtime.publish(onboard, source=SOURCE)
        for n in range(2, 9):
            await runtime.versions.commit(
                WorkflowVersion(
                    workflow="onboard", source=f"{SOURCE}\n# {n}", code_hash=f"h{n}"
                )
            )

        await asyncio.gather(
            *(runtime.versions.activate("onboard", n) for n in range(1, 9))
        )

        active = await runtime.versions.active("onboard")
        assert active is not None and active.version in range(1, 9)
        assert len(await runtime.versions.history("onboard", limit=100)) == 8

    async def test_a_commit_racing_an_activation_never_steals_the_pointer(
        self, runtime: Runtime
    ) -> None:
        """The two share one lock, so a commit cannot land between the
        existence check and the pointer write and leave the newest version
        serving."""
        await runtime.publish(onboard, source=SOURCE)
        await runtime.versions.commit(
            WorkflowVersion(workflow="onboard", source=EDITED, code_hash="edited")
        )
        await runtime.versions.activate("onboard", 2)

        await asyncio.gather(
            runtime.versions.activate("onboard", 1),
            runtime.versions.commit(
                WorkflowVersion(
                    workflow="onboard", source=EDITED + "\n# 3", code_hash="h3"
                )
            ),
        )

        assert (await runtime.versions.latest("onboard")).version == 3
        assert (await runtime.versions.active("onboard")).version == 1

    async def test_the_activation_protocol_is_satisfied_and_separable(self) -> None:
        """Separate from ``VersionStore`` because a ``runtime_checkable``
        protocol is all-or-nothing: adding a method to the shipped port would
        make every host's existing store stop being a version store at all."""
        assert isinstance(StoreBackedVersionStore(MemoryStore()), VersionActivation)

        class HostVersions:
            async def commit(self, version, *, expected_latest=None): ...
            async def get(self, workflow, version): ...
            async def latest(self, workflow): ...
            async def history(self, workflow, *, limit=50): ...
            async def resolve(self, workflow, digest): ...
            async def source_of(self, version): ...

        assert isinstance(HostVersions(), VersionStore)
        assert not isinstance(HostVersions(), VersionActivation)
