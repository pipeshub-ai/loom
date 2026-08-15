"""One Runtime, four surfaces, one answer.

LOOM is a library; the CLI, the MCP server, and the HTTP API are conveniences
over the same object. That only stays true if they share a port rather than each
re-deriving the same operations, so this module tests the *seam* rather than the
features:

* the port is implemented consistently by every adapter (signatures included),
* the HTTP surface actually goes through the port instead of past it,
* and the same operation returns the same thing whichever surface asks.

The third one is the point. The first two are how it keeps being true — a
signature that drifts silently is exactly the failure mode this suite exists to
catch, because the caller still resolves, still type-checks, and fails only at
the one call site nobody exercises.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.facade import (
    LocalFacade,
    RemoteFacade,
    RuntimeFacade,
    describe_entry,
)
from workflow_builder.identity.facade import AuthorizedFacade
from workflow_builder.identity.principal import Principal
from workflow_builder.state import MemoryStore

SOURCE = Path(__file__).resolve().parent.parent / "src" / "workflow_builder"


@step(name="parity_echo")
async def parity_echo(value: str) -> str:
    return value.upper()


@workflow(name="parity_flow")
async def parity_flow(ctx: Context, payload: str) -> str:
    """Echo, durably."""
    return await ctx.step(parity_echo, payload)


@workflow(name="parity_parked")
async def parity_parked(ctx: Context, _: Any = None) -> str:
    await ctx.wait_for_event("go")
    return "released"


def _runtime() -> Runtime:
    rt = Runtime(store=MemoryStore())
    rt.register_all([parity_flow, parity_parked])
    return rt


@pytest.fixture
def local() -> LocalFacade:
    return LocalFacade(_runtime())


@pytest.fixture
def http():
    """A LoomClient wired to an in-process app, plus the facade behind it."""
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    from workflow_builder.server import LoomClient
    from workflow_builder.server.app import create_app

    facade = LocalFacade(_runtime())
    transport = httpx.ASGITransport(app=create_app(facade))
    client = LoomClient(
        http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
    )
    return client, facade


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


def _protocol_methods() -> list[str]:
    return [
        name
        for name in dir(RuntimeFacade)
        if not name.startswith("_") and callable(getattr(RuntimeFacade, name))
    ]


@pytest.mark.parametrize("adapter", [LocalFacade, RemoteFacade, AuthorizedFacade])
@pytest.mark.parametrize("method", _protocol_methods())
def test_every_adapter_implements_the_whole_port(adapter: type, method: str) -> None:
    assert hasattr(adapter, method), f"{adapter.__name__} is missing {method}()"


@pytest.mark.parametrize("method", _protocol_methods())
def test_adapter_signatures_match_the_port(method: str) -> None:
    """A parameter added to one adapter and not the other is a silent break.

    ``RemoteFacade.start(tags=...)`` that quietly dropped ``tags`` type-checked
    fine and lost data only over HTTP. Comparing signatures is what makes that
    a test failure rather than a support ticket. ``AuthorizedFacade`` is held
    to the same bar: it must never grow a parameter the port lacks, because
    that would mean identity leaking into a call site instead of living on
    the wrapper's own state.
    """
    expected = inspect.signature(getattr(RuntimeFacade, method))
    for adapter in (LocalFacade, RemoteFacade, AuthorizedFacade):
        actual = inspect.signature(getattr(adapter, method))
        assert actual.parameters == expected.parameters, (
            f"{adapter.__name__}.{method}{actual} does not match the port "
            f"{method}{expected}"
        )


def test_the_http_surface_goes_through_the_facade() -> None:
    """P0's whole point: three surfaces, one path to the Runtime.

    A route that reaches for the Runtime directly is how a capability ends up
    implemented twice — and the second copy is always the one that rots.
    """
    body = (SOURCE / "server" / "app.py").read_text(encoding="utf-8")
    routes = body[body.index("app = FastAPI") :]

    assert "facade." in routes
    direct = [
        line.strip()
        for line in routes.splitlines()
        if "runtime." in line and "isinstance" not in line and "#" not in line
    ]
    assert not direct, f"routes bypass the facade: {direct}"


# ---------------------------------------------------------------------------
# Same operation, same answer
# ---------------------------------------------------------------------------


async def test_a_run_reads_the_same_through_every_surface(http) -> None:
    from workflow_builder.mcp_server import tools

    client, facade = http
    started = await client.start("parity_flow", "hello", wait=True)
    run_id = started["run_id"]

    over_http = await client.get(run_id)
    over_facade = await facade.get(run_id)
    over_mcp = json.loads(await tools.get_run_status(facade, run_id))
    embedded = await facade.runtime.get(run_id)

    assert embedded is not None
    for view in (over_http, over_facade, over_mcp):
        assert view["run_id"] == run_id
        assert view["status"] == embedded.status.value
        assert view["output"] == "HELLO"


async def test_the_journal_has_one_shape(http) -> None:
    """Not "similar" shapes — the same keys, from the same function."""
    client, facade = http
    run_id = (await client.start("parity_flow", "x", wait=True))["run_id"]

    over_http = await client.journal(run_id)
    over_facade = await facade.journal(run_id)

    assert over_http == over_facade
    entries = await facade.runtime.history(run_id)
    assert over_facade == [describe_entry(entry) for entry in entries]


async def test_the_journal_names_a_step_both_ways(local: LocalFacade) -> None:
    """``step_id`` for the CLI, ``name`` for the HTTP clients that predate it."""
    run_id = (await local.start("parity_flow", "x"))["run_id"]
    entry = (await local.journal(run_id))[0]

    assert entry["step_id"] == entry["name"] == "parity_echo"
    assert entry["output"] == "X"


async def test_tags_and_metadata_survive_the_http_round_trip(http) -> None:
    """They were dropped on the async path before the facade owned ``start``."""
    client, facade = http
    started = await client.start(
        "parity_flow", "x", tags=["urgent"], metadata={"caller": "test"}, wait=False
    )

    record = await facade.runtime.get(started["run_id"])
    assert record is not None
    assert record.tags == ["urgent"]
    assert record.metadata["caller"] == "test"


async def test_unpublished_listing_narrows_to_what_can_run(http) -> None:
    client, facade = http
    await facade.runtime.publish(parity_flow)

    listed = await client.workflows(published=False)
    assert {entry["name"] for entry in listed} == {"parity_flow", "parity_parked"}
    assert all(entry["executable"] for entry in listed)


async def test_an_event_delivered_over_http_advances_the_run(http) -> None:
    client, facade = http
    parked = await client.start("parity_parked", None, wait=True)
    assert parked["status"] == "suspended"

    await client.send_event(parked["run_id"], "go", None)

    final = await facade.get(parked["run_id"])
    assert final is not None
    assert final["status"] == "completed"
    assert final["output"] == "released"


async def test_a_missing_run_is_absent_everywhere(http) -> None:
    from workflow_builder.server import LoomClientError

    client, facade = http

    assert await facade.get("run_nope") is None
    for call in (client.get, client.journal, client.cancel, client.replay):
        with pytest.raises(LoomClientError) as caught:
            await call("run_nope")
        assert caught.value.status_code == 404, call.__name__


class TestSchedulingIsOnThePort:
    """P2's first slice: the control plane is the facade, not a second protocol.

    The manager tools took a ``Runtime`` and reached into it — ``_workflows``
    twice, and stashing a dispatcher on ``_dispatcher``. Poking underscored
    attributes on somebody else's object is what a missing boundary looks like.

    The plan proposed a new ``ControlPlane`` protocol; the facade already was
    one, missing only these three operations. A second protocol beside it would
    have recreated the CLI/MCP split P0 removed.
    """

    async def test_a_schedule_round_trips(self, local: LocalFacade) -> None:
        made = await local.schedule("parity_flow", "0 9 * * *")

        assert made["workflow"] == "parity_flow"
        assert made["next_fire_at"], "the spec computes the first fire time"
        assert [s["workflow"] for s in await local.schedules()] == ["parity_flow"]

    async def test_schedules_filter_by_workflow(self, local: LocalFacade) -> None:
        await local.schedule("parity_flow", "0 9 * * *")

        assert len(await local.schedules("parity_flow")) == 1
        assert await local.schedules("parity_parked") == []

    async def test_unscheduling_reports_whether_it_did_anything(
        self, local: LocalFacade
    ) -> None:
        """``True``/``False``, not silence — a caller retrying needs to know."""
        made = await local.schedule("parity_flow", "0 9 * * *")

        assert await local.unschedule(made["trigger_id"]) is True
        assert await local.unschedule(made["trigger_id"]) is False
        assert await local.schedules() == []

    async def test_an_unknown_workflow_cannot_be_scheduled(
        self, local: LocalFacade
    ) -> None:
        from workflow_builder.core.exceptions import RegistryError

        with pytest.raises(RegistryError):
            await local.schedule("no_such_flow", "0 9 * * *")

    async def test_the_dispatcher_is_not_stashed_on_the_runtime(
        self, local: LocalFacade
    ) -> None:
        """Scheduling is the control plane's business, not the Runtime's."""
        await local.schedule("parity_flow", "0 9 * * *")

        assert not hasattr(local.runtime, "_dispatcher")

    async def test_remote_scheduling_says_where_to_go(self) -> None:
        """Refusing with a route beats a NotImplementedError."""
        from workflow_builder.core.exceptions import ConfigurationError

        remote = RemoteFacade(client=object())
        for call in (
            remote.schedules(),
            remote.schedule("x", "0 9 * * *"),
            remote.unschedule("trg_1"),
        ):
            with pytest.raises(ConfigurationError, match="drop --server"):
                await call


class TestWorkflowManagerAgent:
    """P3: the component that was only ever a cookbook.

    It holds a facade, never a Runtime — so it can start a run and read a
    journal, and has no path to executing arbitrary code in the host process.
    That property is the reason the tools moved off the Runtime at all.
    """

    def _manager(self, runtime):
        from workflow_builder.agents.workflow_tools import WorkflowManagerAgent

        return WorkflowManagerAgent(LocalFacade(runtime), model=object())

    def test_it_holds_no_runtime(self) -> None:
        rt = _runtime()
        manager = self._manager(rt)

        assert isinstance(manager.facade, LocalFacade)
        assert not any(
            isinstance(getattr(manager, name, None), Runtime)
            for name in vars(manager)
        ), "a manager that holds a Runtime can execute a body in-process"

    def test_it_exposes_the_seven_management_tools(self) -> None:
        agent = self._manager(_runtime()).build_agent()

        assert {t.name for t in agent.tools} == {
            "list_workflows",
            "get_workflow_info",
            "run_workflow",
            "list_runs",
            "get_run_status",
            "cancel_run",
            "schedule_workflow",
        }

    def test_its_loop_is_swappable(self) -> None:
        """Same rule as the coding agent: the turn loop is not LOOM's business."""
        from workflow_builder.agents.workflow_tools import WorkflowManagerAgent

        sentinel = object()
        agent = WorkflowManagerAgent(
            LocalFacade(_runtime()), model=object(), executor=sentinel
        ).build_agent()

        assert agent.executor is sentinel

    async def test_the_tools_work_through_the_facade(self) -> None:
        """End to end, without a model: the tools are the part that must work."""
        import json

        from workflow_builder.agents.workflow_tools import build_workflow_tools

        rt = _runtime()
        tools = {fn.__name__: fn for fn in build_workflow_tools(LocalFacade(rt))}

        listed = json.loads(await tools["list_workflows"]())
        assert {w["name"] for w in listed} == {"parity_flow", "parity_parked"}

        started = json.loads(await tools["run_workflow"]("parity_flow", '"hi"'))
        assert started["status"] == "completed"

        status = json.loads(await tools["get_run_status"](started["run_id"]))
        assert status["workflow"] == "parity_flow"

        scheduled = json.loads(
            await tools["schedule_workflow"]("parity_flow", "0 9 * * *")
        )
        assert scheduled["trigger_id"].startswith("trg")

    async def test_a_runtime_is_still_accepted_and_wrapped(self) -> None:
        """Existing callers keep working; a facade is what to pass."""
        from workflow_builder.agents.workflow_tools import build_workflow_tools

        assert len(build_workflow_tools(_runtime())) == 7


class TestAuthorizedFacade:
    """P3: identity wraps the port instead of changing it.

    Every check below exercises ``AuthorizedFacade`` over a plain
    ``LocalFacade`` — the same wrapper works over ``RemoteFacade`` because it
    never reaches past ``self.inner``, which is exactly what the signature
    parity tests above are guarding.
    """

    def _principal(self, subject: str, *scopes: str) -> Principal:
        return Principal(subject=subject, scopes=frozenset(scopes))

    async def test_a_missing_scope_is_refused_before_the_call_happens(self) -> None:
        from workflow_builder.core.exceptions import InsufficientScope

        facade = AuthorizedFacade(LocalFacade(_runtime()), self._principal("alice"))
        with pytest.raises(InsufficientScope):
            await facade.start("parity_flow", "x")

    async def test_a_run_is_pinned_to_the_principal_that_started_it(self) -> None:
        rt = _runtime()
        facade = AuthorizedFacade(
            LocalFacade(rt), self._principal("alice", "runs:write", "runs:read")
        )
        started = await facade.start("parity_flow", "x")

        record = await rt.get(started["run_id"])
        assert record is not None
        assert record.metadata["loom.principal"] == "alice"

    async def test_a_stranger_sees_the_run_exists_but_not_its_output(self) -> None:
        rt = _runtime()
        alice = AuthorizedFacade(
            LocalFacade(rt), self._principal("alice", "runs:write", "runs:read")
        )
        bob = AuthorizedFacade(
            LocalFacade(rt), self._principal("bob", "runs:write", "runs:read")
        )
        started = await alice.start("parity_flow", "x")

        seen_by_bob = await bob.get(started["run_id"])
        assert seen_by_bob is not None
        assert seen_by_bob["run_id"] == started["run_id"]
        assert seen_by_bob["output"] is None

        seen_by_alice = await alice.get(started["run_id"])
        assert seen_by_alice["output"] == "X"

    async def test_an_idempotent_replay_by_a_stranger_does_not_leak_output(
        self,
    ) -> None:
        """The corner case the plan names by name: a second principal hitting
        the same idempotency key gets the original run's shape, not its data."""
        rt = _runtime()
        alice = AuthorizedFacade(
            LocalFacade(rt), self._principal("alice", "runs:write", "runs:read")
        )
        bob = AuthorizedFacade(
            LocalFacade(rt), self._principal("bob", "runs:write", "runs:read")
        )
        await alice.start("parity_flow", "secret-x", idempotency_key="shared-key")
        replayed = await bob.start("parity_flow", "secret-x", idempotency_key="shared-key")

        assert replayed["output"] is None
        assert replayed["input"] is None

    async def test_a_stranger_cannot_retry_or_replay_someone_elses_run(self) -> None:
        from workflow_builder.core.exceptions import InsufficientScope

        rt = _runtime()
        alice = AuthorizedFacade(
            LocalFacade(rt), self._principal("alice", "runs:write", "runs:read")
        )
        bob = AuthorizedFacade(
            LocalFacade(rt), self._principal("bob", "runs:write", "runs:read")
        )
        started = await alice.start("parity_flow", "x")

        for call in (bob.retry, bob.replay, bob.journal, bob.reports):
            with pytest.raises(InsufficientScope):
                await call(started["run_id"])

    async def test_an_admin_scope_sees_and_acts_on_every_run(self) -> None:
        rt = _runtime()
        alice = AuthorizedFacade(
            LocalFacade(rt), self._principal("alice", "runs:write", "runs:read")
        )
        admin = AuthorizedFacade(LocalFacade(rt), self._principal("root", "admin"))
        started = await alice.start("parity_flow", "x")

        seen = await admin.get(started["run_id"])
        assert seen["output"] == "X"
        await admin.journal(started["run_id"])  # does not raise

    async def test_a_run_with_no_pinned_owner_is_ownerless_not_forbidden(self) -> None:
        """Backward compatibility: a record created before identity existed
        (or directly against a bare Runtime) has no ``loom.principal`` key
        and must stay visible, not start failing once this wrapper appears.
        """
        rt = _runtime()
        await rt.run("parity_flow", "legacy")
        [record] = await rt.list_runs()

        bob = AuthorizedFacade(
            LocalFacade(rt), self._principal("bob", "runs:read")
        )
        seen = await bob.get(record.run_id)
        assert seen is not None
        assert seen["output"] == "LEGACY"

    async def test_cancel_is_scope_gated_not_owner_gated(self) -> None:
        """Matches the existing role-based ``FLOW_CANCEL``: an operator with
        the scope can cancel any run, not only ones it started."""
        rt = _runtime()
        alice = AuthorizedFacade(
            LocalFacade(rt), self._principal("alice", "runs:write", "runs:read")
        )
        bob = AuthorizedFacade(LocalFacade(rt), self._principal("bob", "runs:cancel"))
        started = await alice.start("parity_parked", None)
        assert started["status"] == "suspended"

        cancelled = await bob.cancel(started["run_id"])
        assert cancelled["status"] == "cancelled"
