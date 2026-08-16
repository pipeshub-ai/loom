"""One behavioural suite every ``WorkflowRegistry`` must pass.

Two implementations ship — one process-local, one over any ``CacheStore`` — and
a host with its own catalog table writes a third. The catalog is what ties a
finished run to the code that produced it, so the ways these can disagree are
the ways provenance quietly stops being true: a registry that answers ``get``
with a different version than its sibling would attribute a run to code that
never ran.

Everything here goes through the protocol's four methods. Nothing touches an
implementation's internals, so a host can point this suite at its own class.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom.runtime.registry import (
    InMemoryWorkflowRegistry,
    StoreBackedWorkflowRegistry,
    WorkflowRecord,
)
from loom.stores.memory import MemoryStore

EPOCH = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def a_record(name: str = "settle", version: str = "1", **overrides: Any) -> WorkflowRecord:
    """A record with an explicit timestamp.

    Explicit because "most recently published" is resolved by comparing these,
    and a default of ``now()`` makes the ordering depend on how fast the test
    machine constructs objects.
    """
    fields: dict[str, Any] = {
        "name": name,
        "version": version,
        "code_hash": f"hash-of-{name}-{version}",
        "description": f"{name} v{version}",
        "published_at": EPOCH,
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "triggers": ["manual"],
        "tags": ["billing"],
        "metadata": {"team": "payments"},
    }
    fields.update(overrides)
    return WorkflowRecord(**fields)


REGISTRIES: dict[str, Any] = {
    "memory": InMemoryWorkflowRegistry,
    "store-backed": lambda: StoreBackedWorkflowRegistry(MemoryStore()),
}


@pytest.fixture(params=sorted(REGISTRIES))
def registry(request) -> Any:
    return REGISTRIES[request.param]()


class TestEveryRegistryAgrees:
    async def test_a_record_survives_the_round_trip(self, registry) -> None:
        """Whole, not nearly.

        One implementation stores the object and the other serialises it, so
        this is where a field silently stops making the journey — and the fields
        most likely to be dropped, ``code_hash`` and ``input_schema``, are the
        two the catalog exists to carry.
        """
        original = a_record()
        await registry.put(original)

        stored = await registry.get("settle")

        assert stored is not None
        assert stored.name == original.name
        assert stored.version == original.version
        assert stored.code_hash == original.code_hash
        assert stored.input_schema == original.input_schema
        assert stored.triggers == original.triggers
        assert stored.tags == original.tags
        assert stored.metadata == original.metadata
        assert stored.published_at == original.published_at

    async def test_an_unknown_name_is_none_not_an_error(self, registry) -> None:
        assert await registry.get("never-published") is None

    async def test_an_unknown_version_of_a_known_name_is_none(self, registry) -> None:
        await registry.put(a_record(version="1"))

        assert await registry.get("settle", "99") is None

    async def test_a_version_can_be_addressed_exactly(self, registry) -> None:
        await registry.put(a_record(version="1"))
        await registry.put(a_record(version="2", published_at=EPOCH + timedelta(days=1)))

        assert (await registry.get("settle", "1")).description == "settle v1"
        assert (await registry.get("settle", "2")).description == "settle v2"

    async def test_no_version_means_most_recently_published(self, registry) -> None:
        """Not the highest version — the newest.

        Versions are strings, so "10" sorts before "9"; publication time is the
        only ordering that means what the caller asked for.
        """
        await registry.put(a_record(version="9", published_at=EPOCH))
        await registry.put(a_record(version="10", published_at=EPOCH + timedelta(days=1)))

        assert (await registry.get("settle")).version == "10"

    async def test_republishing_the_same_version_replaces_it(self, registry) -> None:
        """Same ``name@version`` is one entry, not two. A registry that appended
        would grow a duplicate on every redeploy of an unchanged workflow."""
        await registry.put(a_record(code_hash="before"))
        await registry.put(a_record(code_hash="after"))

        assert (await registry.get("settle")).code_hash == "after"
        assert len(await registry.list()) == 1

    async def test_list_is_ordered_by_name_then_version(self, registry) -> None:
        """A stable order, so a UI listing the catalog does not reshuffle
        between two identical requests served by different processes."""
        await registry.put(a_record(name="beta", version="1"))
        await registry.put(a_record(name="alpha", version="2"))
        await registry.put(a_record(name="alpha", version="1"))

        assert [r.key for r in await registry.list()] == [
            "alpha@1",
            "alpha@2",
            "beta@1",
        ]

    async def test_deleting_one_version_leaves_the_others(self, registry) -> None:
        await registry.put(a_record(version="1"))
        await registry.put(a_record(version="2"))

        await registry.delete("settle", "1")

        assert [r.key for r in await registry.list()] == ["settle@2"]
        assert await registry.get("settle", "1") is None

    async def test_deleting_with_no_version_removes_every_version(
        self, registry
    ) -> None:
        await registry.put(a_record(version="1"))
        await registry.put(a_record(version="2"))

        await registry.delete("settle")

        assert await registry.list() == []
        assert await registry.get("settle") is None

    async def test_deleting_an_unknown_name_is_accepted(self, registry) -> None:
        """Deletion races publication, and a catalog cleanup that raised on an
        entry someone else already removed would fail a job for succeeding."""
        await registry.delete("never-published")

    async def test_a_deleted_name_can_be_published_again(self, registry) -> None:
        """The case an index-keeping implementation gets wrong: delete has to
        leave the registry in a state that accepts the same key back, not one
        where the entry is written and then never listed."""
        await registry.put(a_record(code_hash="first-life"))
        await registry.delete("settle")
        await registry.put(a_record(code_hash="second-life"))

        assert (await registry.get("settle")).code_hash == "second-life"
        assert [r.key for r in await registry.list()] == ["settle@1"]

    async def test_names_are_not_confused_by_a_shared_prefix(self, registry) -> None:
        """``settle`` and ``settle_invoice`` are different workflows. An
        implementation matching on prefix — or splitting a composite key on the
        wrong separator — would delete one while deleting the other."""
        await registry.put(a_record(name="settle"))
        await registry.put(a_record(name="settle_invoice"))

        await registry.delete("settle")

        assert [r.key for r in await registry.list()] == ["settle_invoice@1"]

    async def test_an_empty_registry_lists_nothing(self, registry) -> None:
        assert await registry.list() == []
