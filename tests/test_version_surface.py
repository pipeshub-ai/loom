"""Reading the version chain, and choosing which entry is served.

Versions were Runtime-only: `rt.versions` and nothing else. Committing already
had a surface through `rt.publish`; what had none at all was *reading the chain*
and *choosing what is live* — which is the half a control plane needs. Without
it, "roll back to version 3" means re-committing version 3's source as version
6, which loses the fact of what is actually being served and inflates the chain
with duplicates of code that already exists.

`VersionSurface` is optional and separate from `RuntimeFacade` for the reason
`GraphProjection` and `IndexedScans` are: a `runtime_checkable` Protocol is
all-or-nothing, so a member added to the required one stops every host's
existing facade from being a facade at all.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, workflow
from loom.core.exceptions import RegistryError
from loom.facade import LocalFacade, VersionSurface
from loom.runtime.versions import UnknownVersion
from loom.stores.memory import MemoryStore

SOURCE = '''
from loom import Context, workflow


@workflow(name="billing")
async def billing(ctx: Context, amount: int) -> int:
    return amount
'''

EDITED = SOURCE.replace("return amount", "return amount * 2")


@workflow(name="billing")
async def billing(ctx: Context, amount: int) -> int:
    return amount


@pytest.fixture
async def facade():
    rt = Runtime(store=MemoryStore())
    rt.register(billing)
    await rt.publish(billing, source=SOURCE)
    await rt.publish(billing, source=EDITED)
    try:
        yield LocalFacade(runtime=rt)
    finally:
        await rt.shutdown()


class TestTheProtocolIsOptional:
    def test_local_facade_provides_it(self) -> None:
        assert isinstance(LocalFacade(runtime=Runtime(store=MemoryStore())), VersionSurface)

    def test_a_facade_without_it_is_not_forced_to_have_it(self) -> None:
        class HostFacade:
            async def workflows(self, *, published: bool = True): ...

        assert not isinstance(HostFacade(), VersionSurface)


class TestReadingTheChain:
    async def test_it_lists_newest_first(self, facade) -> None:
        chain = await facade.versions("billing")

        assert [v["version"] for v in chain] == [2, 1]

    async def test_exactly_one_entry_is_marked_active(self, facade) -> None:
        """`active` is the question a control plane asks, so the answer has to
        be on the row rather than derived by the client from `max(version)` —
        that derivation *is* the conflation the pointer exists to prevent."""
        chain = await facade.versions("billing")

        assert [v["active"] for v in chain] == [False, True]

    async def test_both_hashes_are_carried(self, facade) -> None:
        """They answer different questions — "show me this version's source"
        and "which version produced this run" — and dropping either leaves one
        of them unanswerable."""
        newest = (await facade.versions("billing"))[0]

        assert newest["content_hash"]
        assert "code_hash" in newest

    async def test_source_comes_back_verbatim(self, facade) -> None:
        assert await facade.version_source("billing", 1) == SOURCE
        assert await facade.version_source("billing", 2) == EDITED

    async def test_an_unknown_version_says_so(self, facade) -> None:
        with pytest.raises(RegistryError):
            await facade.version_source("billing", 99)


class TestActivating:
    async def test_it_moves_the_pointer(self, facade) -> None:
        served = await facade.activate_version("billing", 2)

        assert served["version"] == 2 and served["active"] is True
        assert [v["active"] for v in await facade.versions("billing")] == [True, False]

    async def test_rolling_back_commits_nothing(self, facade) -> None:
        """The whole point: a rollback is a pointer move, not a re-commit."""
        await facade.activate_version("billing", 2)
        await facade.activate_version("billing", 1)

        chain = await facade.versions("billing")

        assert [v["version"] for v in chain] == [2, 1], "no third version"
        assert chain[1]["active"] is True

    async def test_activating_twice_is_harmless(self, facade) -> None:
        """A pointer move is naturally idempotent, which is what makes POST safe
        for an operator who cannot tell whether the first request landed."""
        first = await facade.activate_version("billing", 2)
        second = await facade.activate_version("billing", 2)

        assert first == second

    async def test_an_unknown_version_is_refused(self, facade) -> None:
        with pytest.raises(UnknownVersion):
            await facade.activate_version("billing", 99)


class TestOverHttp:
    @pytest.fixture
    def client(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from loom.server.app import create_app

        rt = Runtime(store=MemoryStore())
        rt.register(billing)
        return TestClient(create_app(rt)), rt

    async def _seed(self, rt) -> None:
        await rt.publish(billing, source=SOURCE)
        await rt.publish(billing, source=EDITED)

    def test_the_chain_is_served(self, client) -> None:
        http, rt = client
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(self._seed(rt))

        response = http.get("/workflows/billing/versions")

        assert response.status_code == 200
        assert [v["version"] for v in response.json()] == [2, 1]

    def test_activation_is_a_post(self, client) -> None:
        http, rt = client
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(self._seed(rt))

        response = http.post("/workflows/billing/versions/2/activate")

        assert response.status_code == 200
        assert response.json()["version"] == 2
        assert response.json()["active"] is True

    def test_source_is_served(self, client) -> None:
        http, rt = client
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(self._seed(rt))

        response = http.get("/workflows/billing/versions/1/source")

        assert response.status_code == 200
        assert response.json()["source"] == SOURCE
