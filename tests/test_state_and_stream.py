"""Workflow state that outlives a run, and output that streams while one lasts.

Both exist because the journal deliberately answers neither question. State is
shared across runs, so it cannot be journaled without a replay reading the past
and calling it the present; reports are observations *about* a run, so
journaling them would make progress chatter part of the replay contract.

Those two properties are what most of this file tests. The rest is the rename:
``ctx.emit`` meant two things, and now it means one and warns.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.core.exceptions import RegistryError
from workflow_builder.facade import LocalFacade
from workflow_builder.runtime.state import (
    InMemoryRunStream,
    Report,
    RunStream,
    StateStore,
    StoreBackedState,
)
from workflow_builder.state import MemoryStore, SQLiteStore


@workflow(name="state_counter")
async def state_counter(ctx: Context, _: Any = None) -> int:
    seen = await ctx.state.get("runs", default=0)
    await ctx.state.set("runs", seen + 1)
    return seen + 1


@step(name="state_slow")
async def state_slow() -> str:
    return "done"


@workflow(name="state_talker")
async def state_talker(ctx: Context, _: Any = None) -> str:
    await ctx.report("fetching page 1")
    result = await ctx.step(state_slow)
    await ctx.report("indexing", kind="progress")
    return result


# ---------------------------------------------------------------------------
# The ports
# ---------------------------------------------------------------------------


def test_both_ports_have_a_reference_adapter() -> None:
    """Design rule 1, and rule 2: the default needs no infrastructure."""
    assert isinstance(StoreBackedState(MemoryStore()), StateStore)
    assert isinstance(InMemoryRunStream(), RunStream)

    rt = Runtime()
    assert isinstance(rt.state, StoreBackedState)
    assert isinstance(rt.stream, InMemoryRunStream)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """A guarantee that holds in only one store is not a guarantee."""
    if request.param == "memory":
        return MemoryStore()
    return SQLiteStore(tmp_path / "runs.db")


async def test_state_round_trips_through_every_store(store) -> None:
    state = StoreBackedState(store)

    await state.set("flow", "cursor", {"page": 3})
    assert await state.get("flow", "cursor") == {"page": 3}
    assert await state.keys("flow") == ["cursor"]

    await state.delete("flow", "cursor")
    assert await state.get("flow", "cursor") is None
    assert await state.keys("flow") == []


async def test_state_does_not_expire(store) -> None:
    """It is state, not a cache.

    Backed by a ``CacheStore``, which is the thing most likely to be given a
    default TTL by someone tidying up. A workflow's memory of the last cursor it
    saw must not depend on how long it went unused.
    """
    state = StoreBackedState(store)
    await state.set("flow", "cursor", 1)

    raw_key = StoreBackedState._key("flow", "cursor")
    assert await store.get(raw_key) is not None


# ---------------------------------------------------------------------------
# State through a run
# ---------------------------------------------------------------------------


async def test_state_survives_across_runs_of_one_workflow() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register(state_counter)

    assert (await rt.run(state_counter)).output == 1
    assert (await rt.run(state_counter)).output == 2
    assert (await rt.run(state_counter)).output == 3


async def test_state_is_not_shared_between_workflows() -> None:
    @workflow(name="state_other")
    async def other(ctx: Context, _: Any = None) -> int:
        return await ctx.state.get("runs", default=0)

    rt = Runtime(store=MemoryStore())
    rt.register_all([state_counter, other])

    await rt.run(state_counter)
    assert (await rt.run(other)).output == 0


async def test_a_state_write_is_not_journaled() -> None:
    """Which is why the docstring warns about branching on it.

    Asserted rather than described: a future change that started journaling
    state reads would make replays diverge from what actually happened, and the
    only signal would be a workflow that quietly takes a different branch.
    """
    rt = Runtime(store=MemoryStore())
    rt.register(state_counter)
    result = await rt.run(state_counter)

    assert await rt.history(result.run_id) == []


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


async def test_a_run_can_narrate_itself() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register(state_talker)
    result = await rt.run(state_talker)

    said = rt.stream.since(result.run_id)
    assert [report.message for report in said] == ["fetching page 1", "indexing"]
    assert [report.kind for report in said] == ["text", "progress"]
    assert all(report.run_id == result.run_id for report in said)


async def test_reports_are_not_journaled_and_a_replay_reports_as_itself() -> None:
    """Not journaling reports has a consequence worth pinning down.

    The body really runs on replay, so it really reports again. That is right —
    a replay is a real execution and a watcher should see it move — and it is
    safe because the reports carry the replay's own run id, so they cannot be
    read as the original's.
    """
    rt = Runtime(store=MemoryStore())
    rt.register(state_talker)
    original = await rt.run(state_talker)

    assert [entry.name for entry in await rt.history(original.run_id)] == ["state_slow"]
    assert len(rt.stream.since(original.run_id)) == 2

    replayed = await rt.replay(original.run_id)

    assert replayed.run_id != original.run_id
    assert len(rt.stream.since(replayed.run_id)) == 2
    # The original's stream is untouched: nothing was appended to it.
    assert len(rt.stream.since(original.run_id)) == 2


async def test_the_buffer_is_bounded() -> None:
    """A stream is a convenience for watching, not a second journal."""
    stream = InMemoryRunStream(per_run=3)
    for index in range(10):
        await stream.report("run_1", f"step {index}")

    assert [report.message for report in stream.since("run_1")] == [
        "step 7",
        "step 8",
        "step 9",
    ]


async def test_a_watcher_can_wait_rather_than_spin() -> None:
    import asyncio

    stream = InMemoryRunStream()
    waiting = asyncio.ensure_future(stream.wait("run_1", timeout=2.0))
    await asyncio.sleep(0)
    await stream.report("run_1", "something happened")
    await asyncio.wait_for(waiting, timeout=1.0)

    assert len(stream.since("run_1")) == 1


async def test_waiting_on_a_silent_run_times_out_rather_than_hanging() -> None:
    await InMemoryRunStream().wait("run_quiet", timeout=0.05)


def test_a_report_describes_itself_for_the_wire() -> None:
    described = Report(run_id="run_1", message="hi", kind="progress").describe()
    assert described["run_id"] == "run_1"
    assert described["message"] == "hi"
    assert described["kind"] == "progress"
    assert described["at"].endswith("+00:00")


# ---------------------------------------------------------------------------
# Reports through the surfaces
# ---------------------------------------------------------------------------


async def test_the_facade_serves_reports() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register(state_talker)
    base = LocalFacade(rt)

    run_id = (await base.start("state_talker", None))["run_id"]

    assert [r["message"] for r in await base.reports(run_id)] == [
        "fetching page 1",
        "indexing",
    ]
    assert [r["message"] for r in await base.reports(run_id, 1)] == ["indexing"]

    with pytest.raises(RegistryError):
        await base.reports("run_nope")


async def test_a_host_stream_that_cannot_be_read_back_says_so_honestly() -> None:
    """Empty, not an error — but never a fabricated answer.

    A host adapter that fans out to a websocket has nowhere to read from. The
    facade returns nothing rather than pretending the run was silent *or*
    raising, because a caller polling for progress should degrade to no
    progress, not to a broken run.
    """

    class WriteOnly:
        async def report(self, run_id: str, message: str, *, kind: str = "text") -> None:
            return None

    rt = Runtime(store=MemoryStore(), stream=WriteOnly())
    rt.register(state_talker)
    facade = LocalFacade(rt)
    run_id = (await facade.start("state_talker", None))["run_id"]

    assert await facade.reports(run_id) == []


async def test_progress_is_visible_over_http_and_mcp() -> None:
    import json

    httpx = pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    from workflow_builder.mcp_server import tools
    from workflow_builder.server import LoomClient
    from workflow_builder.server.app import create_app

    rt = Runtime(store=MemoryStore())
    rt.register(state_talker)
    facade = LocalFacade(rt)
    client = LoomClient(
        http=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(facade)),
            base_url="http://loom.test",
        )
    )

    run_id = (await client.start("state_talker", None, wait=True))["run_id"]

    over_http = await client.reports(run_id)
    assert [r["message"] for r in over_http] == ["fetching page 1", "indexing"]
    assert await client.reports(run_id, offset=2) == []

    over_mcp = json.loads(await tools.get_run_progress(facade, run_id))
    assert over_mcp["count"] == 2
    assert over_mcp["next_offset"] == 2

    unknown = json.loads(await tools.get_run_progress(facade, "run_nope"))
    assert "error" in unknown


async def test_loom_watch_shows_what_a_run_narrated(capsys) -> None:
    """The exit criterion, through the code ``loom watch`` actually runs."""
    from workflow_builder.cli.commands import follow
    from workflow_builder.cli.output import Printer

    rt = Runtime(store=MemoryStore())
    rt.register(state_talker)
    facade = LocalFacade(rt)
    run_id = (await facade.start("state_talker", None))["run_id"]

    await follow(facade, run_id, Printer(as_json=False), timeout=5.0)

    printed = capsys.readouterr().out
    assert "fetching page 1" in printed
    assert "indexing" in printed
    assert "state_slow" in printed


# ---------------------------------------------------------------------------
# The rename
# ---------------------------------------------------------------------------


async def test_publish_delivers_an_event_to_a_waiter() -> None:
    rt = Runtime(store=MemoryStore())

    @workflow(name="state_publisher")
    async def publisher(ctx: Context, _: Any = None) -> str:
        await ctx.publish("order.shipped", {"order": 7})
        return "sent"

    @workflow(name="state_waiter")
    async def waiter(ctx: Context, _: Any = None) -> Any:
        return await ctx.wait_for_event("order.shipped")

    rt.register_all([publisher, waiter])

    parked = await rt.run(waiter)
    assert parked.status.value == "suspended"

    await rt.run(publisher)
    resumed = await rt.resume(parked.run_id)
    assert resumed.output == {"order": 7}


async def test_emit_still_works_and_warns_once_per_site() -> None:
    rt = Runtime(store=MemoryStore())

    @workflow(name="state_legacy")
    async def legacy(ctx: Context, _: Any = None) -> str:
        await ctx.emit("thing.happened", {"n": 1})
        return "ok"

    rt.register(legacy)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await rt.run(legacy)

    assert result.output == "ok"
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert "ctx.publish()" in str(deprecations[0].message)
    assert "ctx.report()" in str(deprecations[0].message)


async def test_a_run_started_under_emit_replays_under_publish() -> None:
    """The journal keeps the old entry name, so in-flight runs stay readable.

    Renaming the entry would have made every parked run's journal unreadable to
    the code that has to finish it — a steep price for a tidier string.
    """
    rt = Runtime(store=MemoryStore())

    @workflow(name="state_renamed")
    async def flow(ctx: Context, _: Any = None) -> str:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await ctx.emit("thing.happened", {"n": 1})
        return "ok"

    rt.register(flow)
    original = await rt.run(flow)
    assert [entry.name for entry in await rt.history(original.run_id)] == [
        "emit:thing.happened"
    ]

    # The same journal, re-entered by a deployment whose body calls publish().
    @workflow(name="state_renamed")
    async def flow_v2(ctx: Context, _: Any = None) -> str:
        await ctx.publish("thing.happened", {"n": 1})
        return "ok"

    upgraded = Runtime(store=rt.store)
    upgraded.register(flow_v2)
    replayed = await upgraded.replay(original.run_id)
    assert replayed.status.value == "completed"
