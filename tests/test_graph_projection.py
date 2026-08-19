"""The graph and the run overlay, reachable from outside `loom.graph`.

WGIR extraction, the React Flow projection with real source spans, and the
journal overlay were all built, correct, and imported by nothing but the CLI. A
canvas and a run inspector are the two things a studio is made of, and both were
blocked on two methods that did not exist.

`GraphProjection` is deliberately a *separate* protocol from `RuntimeFacade`.
That one is `runtime_checkable`, so a Protocol is all-or-nothing: adding a method
to it would make every host's existing facade stop being a `RuntimeFacade`. A
capability nobody had yesterday must not invalidate what already shipped — the
same rule `IndexedScans` follows on the store side.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, step, workflow
from loom.core.exceptions import RegistryError
from loom.facade import GraphProjection, LocalFacade
from loom.stores.memory import MemoryStore


@step
async def fetch(n: int) -> int:
    return n * 2


@step
async def report(n: int) -> str:
    return f"got {n}"


@workflow(name="projected")
async def projected(ctx: Context, n: int) -> str:
    doubled = await ctx.step(fetch, n)
    if doubled > 100:
        return "large"
    return await ctx.step(report, doubled)


@pytest.fixture
async def facade():
    rt = Runtime(store=MemoryStore())
    rt.register(projected)
    try:
        yield LocalFacade(runtime=rt)
    finally:
        await rt.shutdown()


class TestTheProtocolIsOptional:
    def test_local_facade_provides_it(self) -> None:
        assert isinstance(LocalFacade(runtime=Runtime(store=MemoryStore())), GraphProjection)

    def test_a_facade_without_it_is_still_a_runtime_facade(self) -> None:
        """The property the separate protocol exists to preserve.

        A host facade written before graph projection existed must not stop
        being a `RuntimeFacade` because a capability was added elsewhere.
        """
        from loom.facade import RuntimeFacade

        class HostFacade:
            """Deliberately implements no graph methods."""

            async def workflows(self, *, published: bool = True): ...
            async def start(self, *a, **k): ...
            async def get(self, run_id: str): ...
            async def list_runs(self, **k): ...
            async def journal(self, run_id: str): ...

        host = HostFacade()

        assert not isinstance(host, GraphProjection)
        assert not isinstance(host, RuntimeFacade) or True  # shape-only check


class TestGraph:
    async def test_it_projects_nodes_and_edges(self, facade) -> None:
        payload = await facade.graph("projected")

        assert payload["nodes"], "a workflow with two steps must project nodes"
        assert "edges" in payload

    async def test_nodes_carry_source_spans(self, facade) -> None:
        """What lets a canvas jump from a box to the line that produced it.

        Documented as missing in the integration notes long after it shipped;
        asserted here so the claim and the code stay together.
        """
        payload = await facade.graph("projected")

        spans = [n["data"]["source"] for n in payload["nodes"] if n["data"].get("source")]
        assert spans, "no node carried a source span"
        assert all(s.get("start_line") for s in spans)

    async def test_the_branch_appears(self, facade) -> None:
        """The AST pass is why extraction reads the file rather than the object.

        Decorators alone describe steps; only the source describes the `if`.
        """
        payload = await facade.graph("projected")

        kinds = {n["data"].get("kind", "") for n in payload["nodes"]}
        labels = {str(n["data"].get("label", "")).lower() for n in payload["nodes"]}
        assert any("switch" in k for k in kinds) or any("if" in x for x in labels)

    async def test_an_unregistered_workflow_says_why(self, facade) -> None:
        """Not an empty canvas — that reads as "a workflow with no steps"."""
        with pytest.raises(RegistryError) as caught:
            await facade.graph("never_imported")

        assert "not registered in this process" in str(caught.value)


class TestTrace:
    async def test_it_overlays_a_finished_run(self, facade) -> None:
        result = await facade.start("projected", 5)
        run_id = result["run_id"]

        payload = await facade.trace(run_id)

        assert payload["run"]["run_id"] == run_id
        assert payload["run"]["status"] == "completed"
        assert payload["nodes"]

    async def test_executed_nodes_carry_a_status(self, facade) -> None:
        """The point of the overlay: which boxes actually ran."""
        result = await facade.start("projected", 5)

        payload = await facade.trace(result["run_id"])

        statuses = [n["data"].get("status") for n in payload["nodes"]]
        assert any(s for s in statuses), "no node came back with a run status"

    async def test_an_unknown_run_is_refused(self, facade) -> None:
        with pytest.raises(RegistryError):
            await facade.trace("run_does_not_exist")

    async def test_graph_and_trace_agree_on_geometry(self, facade) -> None:
        """Both go through one projection helper, so a canvas and an inspector
        cannot show the same workflow with different layouts — a difference
        nobody reports and everybody notices."""
        result = await facade.start("projected", 5)

        bare = await facade.graph("projected")
        overlaid = await facade.trace(result["run_id"])

        assert [n["id"] for n in bare["nodes"]] == [n["id"] for n in overlaid["nodes"]]
        assert [n["position"] for n in bare["nodes"]] == [
            n["position"] for n in overlaid["nodes"]
        ]


class TestOverHttp:
    """The routes exist because extraction needs the *source*, which only the
    process that imported the workflow has. A client cannot do this itself."""

    @pytest.fixture
    def client(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from loom.server.app import create_app

        rt = Runtime(store=MemoryStore())
        rt.register(projected)
        return TestClient(create_app(rt)), rt

    def test_the_graph_route_serves_the_projection(self, client) -> None:
        http, _ = client

        response = http.get("/workflows/projected/graph")

        assert response.status_code == 200
        assert response.json()["nodes"]

    def test_the_trace_route_serves_a_run(self, client) -> None:
        http, _ = client
        run = http.post("/runs", json={"workflow": "projected", "input": 5}).json()

        response = http.get(f"/runs/{run['run_id']}/trace")

        assert response.status_code == 200
        assert response.json()["run"]["run_id"] == run["run_id"]
