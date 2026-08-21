"""MCP server, at three levels.

**Unit** — each capability function against a real ``LocalFacade``, with no
``mcp`` import anywhere. This is the layer that would catch a logic bug.

**Integration** — a real ``MCPServer`` instance driven in process: does it
register everything, are the schemas derived from the signatures, does calling a
tool reach the facade.

**End to end** — a real subprocess speaking stdio, driven by the official MCP
client, doing the full handshake. This is the only level that proves the thing a
client actually connects to works.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from loom import Context, Runtime, step, workflow
from loom.facade import LocalFacade
from loom.mcp_server import prompts, resources, tools
from loom.stores.memory import MemoryStore

pytest.importorskip("mcp", reason="needs the mcp extra")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@step
async def double(n: int) -> int:
    """Double a number."""
    return n * 2


@step(retry=1)
async def boom() -> str:
    """Always raises."""
    raise RuntimeError("upstream is down")


@workflow(name="doubler", description="Double the input")
async def doubler(ctx: Context, n: int) -> int:
    """Double it."""
    return await ctx.step(double, n)


@workflow(name="approver", description="Waits for a human")
async def approver(ctx: Context, _input: str) -> str:
    """Park on an approval."""
    return "yes" if await ctx.wait_for_approval("release") else "no"


@workflow(name="waiter", description="Waits for an event")
async def waiter(ctx: Context, _input: str) -> str:
    """Park on a named event."""
    return str((await ctx.wait_for_event("go")).get("token", ""))


@workflow(name="breaker", description="Always fails")
async def breaker(ctx: Context, _input: str) -> str:
    """Fail, for retry and journal tests."""
    return await ctx.step(boom)


@pytest.fixture
def facade() -> LocalFacade:
    runtime = Runtime(store=MemoryStore())
    runtime.register_all([doubler, approver, waiter, breaker])
    return LocalFacade(runtime)


def parsed(raw: str) -> dict:
    """Every tool returns JSON text; this asserts that and decodes it."""
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Unit — capabilities, no MCP
# ---------------------------------------------------------------------------


class TestToolsUnit:
    async def test_list_workflows(self, facade: LocalFacade) -> None:
        result = parsed(await tools.list_workflows(facade))
        names = {w["name"] for w in result["workflows"]}
        assert {"doubler", "approver", "waiter", "breaker"} <= names

    async def test_empty_server_explains_itself(self) -> None:
        """A client connecting to an empty server should learn why."""
        empty = LocalFacade(Runtime(store=MemoryStore()))
        result = parsed(await tools.list_workflows(empty))

        assert result["workflows"] == []
        assert "--module" in result["hint"]

    async def test_run_workflow(self, facade: LocalFacade) -> None:
        result = parsed(await tools.run_workflow(facade, "doubler", "21"))
        assert result["status"] == "completed"
        assert result["output"] == 42

    async def test_run_unknown_workflow_lists_the_real_ones(
        self, facade: LocalFacade
    ) -> None:
        result = parsed(await tools.run_workflow(facade, "nope"))
        assert "error" in result
        assert "doubler" in result["available"]

    async def test_run_accepts_a_bare_string_input(self, facade: LocalFacade) -> None:
        """Most workflows take a string; requiring '"text"' would be hostile."""
        result = parsed(await tools.run_workflow(facade, "approver", "plain text"))
        assert result["status"] == "suspended"

    async def test_listing_advertises_the_input_shape(
        self, facade: LocalFacade
    ) -> None:
        """Without this a caller guesses, and a guess costs a failed run."""
        result = parsed(await tools.list_workflows(facade))
        by_name = {w["name"]: w for w in result["workflows"]}

        assert by_name["doubler"]["input_schema"]["type"] == "integer"
        assert by_name["doubler"]["input_schema"]["title"] == "n"
        assert by_name["approver"]["input_schema"]["type"] == "string"

    async def test_wrong_input_shape_is_refused_before_running(
        self, facade: LocalFacade
    ) -> None:
        """The mistake a model actually makes: wrapping a scalar in an object."""
        result = parsed(await tools.run_workflow(facade, "doubler", '{"n": 21}'))

        assert "takes integer" in result["error"]
        assert "Nothing was run" in result["error"]
        assert result["expected_schema"]["type"] == "integer"
        assert result["example_input_json"] == "42"

        # And it means it: no run was created.
        assert parsed(await tools.list_runs(facade))["count"] == 0

    async def test_a_valid_shape_still_runs(self, facade: LocalFacade) -> None:
        assert parsed(await tools.run_workflow(facade, "doubler", "21"))["output"] == 42

    async def test_unknown_shapes_are_left_alone(self, facade: LocalFacade) -> None:
        """An undeclared input type is not evidence of a wrong input."""

        @workflow(name="untyped", description="No annotation")
        async def untyped(ctx: Context, anything) -> str:
            return str(anything)

        facade.runtime.register(untyped)
        result = parsed(await tools.run_workflow(facade, "untyped", '{"a": 1}'))
        assert result["status"] == "completed"

    async def test_suspended_result_names_its_next_action(
        self, facade: LocalFacade
    ) -> None:
        """The state a model most reliably misreads as failure."""
        result = parsed(await tools.run_workflow(facade, "approver", '"x"'))

        assert result["status"] == "suspended"
        assert result["waiting_for"] == "human approval 'release'"
        assert "approve_run" in result["next_action"]
        assert "not failure" in result["note"]

    async def test_event_wait_names_send_event(self, facade: LocalFacade) -> None:
        result = parsed(await tools.run_workflow(facade, "waiter", '"x"'))
        assert result["waiting_for"] == "event 'go'"
        assert "send_event" in result["next_action"]

    async def test_completed_runs_are_not_annotated(self, facade: LocalFacade) -> None:
        result = parsed(await tools.run_workflow(facade, "doubler", "1"))
        assert "next_action" not in result

    async def test_get_run_status(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "doubler", "5"))
        status = parsed(await tools.get_run_status(facade, run["run_id"]))
        assert status["output"] == 10

    async def test_unknown_run_is_an_error_payload_not_a_raise(
        self, facade: LocalFacade
    ) -> None:
        """A raise aborts the model's turn; an error payload it can act on."""
        for call in (
            tools.get_run_status(facade, "nope"),
            tools.get_run_journal(facade, "nope"),
            tools.get_run_progress(facade, "nope"),
            tools.cancel_run(facade, "nope"),
            tools.retry_run(facade, "nope"),
            tools.replay_run(facade, "nope"),
            tools.approve_run(facade, "nope", "x"),
            tools.send_event(facade, "nope", "x"),
        ):
            assert "error" in parsed(await call)

    async def test_list_runs_filters(self, facade: LocalFacade) -> None:
        await tools.run_workflow(facade, "doubler", "1")
        await tools.run_workflow(facade, "breaker", '"x"')

        failed = parsed(await tools.list_runs(facade, status="failed"))
        assert [r["workflow"] for r in failed["runs"]] == ["breaker"]

        by_flow = parsed(await tools.list_runs(facade, workflow="doubler"))
        assert by_flow["count"] == 1

    async def test_journal_reports_the_steps_that_ran(
        self, facade: LocalFacade
    ) -> None:
        run = parsed(await tools.run_workflow(facade, "doubler", "3"))
        journal = parsed(await tools.get_run_journal(facade, run["run_id"]))

        assert [e["step_id"] for e in journal["journal"]] == ["double"]
        assert journal["count"] == 1

    async def test_approve_completes_a_parked_run(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "approver", '"x"'))
        result = parsed(await tools.approve_run(facade, run["run_id"], "release"))

        assert result["status"] == "completed"
        assert result["output"] == "yes"

    async def test_reject_takes_the_other_branch(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "approver", '"x"'))
        result = parsed(
            await tools.approve_run(facade, run["run_id"], "release", approved=False)
        )
        assert result["output"] == "no"

    async def test_approving_the_wrong_subject_is_refused(
        self, facade: LocalFacade
    ) -> None:
        """Guessing a subject would silently do nothing; saying so is better."""
        run = parsed(await tools.run_workflow(facade, "approver", '"x"'))
        result = parsed(await tools.approve_run(facade, run["run_id"], "wrong"))

        assert "error" in result
        assert result["awaiting"] == "approval:release"

    async def test_send_event_resumes(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "waiter", '"x"'))
        result = parsed(
            await tools.send_event(facade, run["run_id"], "go", '{"token": "abc"}')
        )
        assert result["output"] == "abc"

    async def test_cancel(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "approver", '"x"'))
        assert parsed(await tools.cancel_run(facade, run["run_id"]))["status"] == (
            "cancelled"
        )

    async def test_replay_reproduces_without_rerunning(
        self, facade: LocalFacade
    ) -> None:
        run = parsed(await tools.run_workflow(facade, "doubler", "8"))
        replayed = parsed(await tools.replay_run(facade, run["run_id"]))

        assert replayed["status"] == "completed"
        assert replayed["output"] == 16

    async def test_retry_reruns_a_failure(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "breaker", '"x"'))
        assert run["status"] == "failed"

        retried = parsed(await tools.retry_run(facade, run["run_id"]))
        assert retried["run_id"] == run["run_id"]

    async def test_idempotency_key_returns_the_same_run(
        self, facade: LocalFacade
    ) -> None:
        first = parsed(
            await tools.run_workflow(facade, "doubler", "2", idempotency_key="k")
        )
        second = parsed(
            await tools.run_workflow(facade, "doubler", "2", idempotency_key="k")
        )
        assert first["run_id"] == second["run_id"]


class TestResponseCaps:
    """A tool's response is capped server-side, with paging discoverable
    through ``next_offset`` — so a busy install's history sizes one tool
    call by what the caller asked to see, not by how much history exists."""

    async def test_small_responses_pass_through_untouched(
        self, facade: LocalFacade
    ) -> None:
        result = parsed(await tools.list_runs(facade))
        assert "truncated" not in result

    async def test_an_oversized_list_is_capped_with_a_next_offset(self) -> None:
        payload = {"runs": [{"run_id": f"r{i}"} for i in range(5000)], "count": 5000}
        capped = tools._cap_list(payload, "runs", 0)

        assert capped["truncated"] is True
        assert len(capped["runs"]) < 5000
        assert capped["next_offset"] == len(capped["runs"])
        assert len(tools._json(capped)) <= tools.MAX_RESPONSE_CHARS

    async def test_a_list_already_under_budget_is_not_marked_truncated(self) -> None:
        payload = {"runs": [{"run_id": "r1"}], "count": 1}
        assert tools._cap_list(payload, "runs", 0) == payload

    async def test_journal_pages_via_offset(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "doubler", "9"))
        first_page = parsed(await tools.get_run_journal(facade, run["run_id"]))
        assert first_page["count"] == 1

        # Asking past the end returns an empty page, not an error — the run
        # exists, offset just names a position beyond what it recorded.
        empty_page = parsed(
            await tools.get_run_journal(facade, run["run_id"], offset=99)
        )
        assert empty_page["journal"] == []

    async def test_an_oversized_single_object_falls_back_to_a_preview(self) -> None:
        payload = {"run_id": "x", "output": "y" * 50_000}
        capped = tools._cap_text(payload)

        assert capped["truncated"] is True
        assert capped["total_chars"] > tools.MAX_RESPONSE_CHARS
        assert len(tools._json(capped)) <= tools.MAX_RESPONSE_CHARS
        # The preview is a plain string field — still valid JSON overall.
        assert json.loads(tools._json(capped))["preview"]


class TestToolsetDiscoveryTools:
    """``search_toolsets``/``show_toolset`` at the MCP layer — the same
    catalog the coding agent's ReAct tools browse, exposed so a client pulls
    integration detail on demand instead of it being preloaded.

    ``loom mcp`` seeds every shipped toolset, so these tests do not register
    Jira by hand — that would hide a regression where the default seed
    stopped running.
    """

    async def test_search_finds_shipped_toolsets(self) -> None:
        result = parsed(await tools.search_toolsets("jira"))
        ids = {c["toolset_id"] for c in result["toolsets"]}
        assert "jira" in ids

    async def test_search_finds_gmail_calendar_and_confluence(self) -> None:
        gmail = parsed(await tools.search_toolsets("gmail"))
        calendar = parsed(await tools.search_toolsets("calendar"))
        confluence = parsed(await tools.search_toolsets("confluence"))
        assert "gmail" in {c["toolset_id"] for c in gmail["toolsets"]}
        assert "google_calendar" in {c["toolset_id"] for c in calendar["toolsets"]}
        assert "confluence" in {c["toolset_id"] for c in confluence["toolsets"]}

    async def test_search_for_nothing_registered_is_empty_not_an_error(self) -> None:
        result = parsed(await tools.search_toolsets("nonexistent_xyz_toolset_99"))
        assert result["toolsets"] == []

    async def test_show_lists_operations_for_a_shipped_toolset(self) -> None:
        result = parsed(await tools.show_toolset("jira"))
        assert result["toolset_id"] == "jira"
        assert len(result["ops"]) >= 1

    async def test_show_unknown_toolset_is_an_error_payload_not_a_raise(self) -> None:
        result = parsed(await tools.show_toolset("does-not-exist"))
        assert "error" in result

    async def test_both_are_registered_as_mcp_tools(self, server) -> None:
        names = {t.name for t in await server.list_tools()}
        assert {"search_toolsets", "show_toolset"} <= names


class TestSchemaBudget:
    """A verbose docstring is a context tax paid on every turn a model holds
    this server's tools in scope — this makes that a failing test instead of
    a silent regression.

    Raised twice, each time for a *count* rather than for verbosity: from
    12,000 to 18,000 when the six authoring tools joined the sixteen
    run-management ones, and from 18,000 to 24,000 when thirteen more closed
    the gap between what ``RuntimeFacade`` can do and what an MCP client could
    reach — ``edit_workflow`` most of all, which was reachable only from the
    CLI.

    Which is why the mean matters more than the total, and is asserted
    separately. A total is the only number a *user* pays, but it moves whenever
    the surface grows, so a ceiling on it either freezes the surface or gets
    raised without anyone reading it. The mean is what catches the actual
    regression this exists for: a description that drifts into explaining
    itself to a human. Design history belongs in ``CLAUDE.md``; what stays here
    is what a model needs to choose the tool and call it correctly.
    """

    MAX_TOTAL_SCHEMA_CHARS = 24_000
    #: Measured mean is ~530. A tool needing much more than this is usually one
    #: whose docstring is arguing rather than instructing.
    MAX_MEAN_SCHEMA_CHARS = 650

    @staticmethod
    def _size(tool) -> int:
        return len(tool.name) + len(tool.description or "") + len(
            json.dumps(tool.input_schema)
        )

    async def test_total_tool_schema_size_stays_under_budget(self, server) -> None:
        registered = await server.list_tools()
        total = sum(self._size(t) for t in registered)
        assert total <= self.MAX_TOTAL_SCHEMA_CHARS, (
            f"{len(registered)} tools' schemas total {total} chars, over the "
            f"{self.MAX_TOTAL_SCHEMA_CHARS} budget"
        )

    async def test_the_average_tool_stays_terse(self, server) -> None:
        """Independent of how many tools there are, which is the point."""
        registered = await server.list_tools()
        mean = sum(self._size(t) for t in registered) / max(1, len(registered))
        worst = max(registered, key=self._size)
        assert mean <= self.MAX_MEAN_SCHEMA_CHARS, (
            f"mean schema is {mean:.0f} chars over {len(registered)} tools "
            f"(worst: {worst.name} at {self._size(worst)}). Trim the prose — "
            "rationale belongs in CLAUDE.md, not in a schema sent every turn."
        )


class TestResourcesUnit:
    async def test_workflows_document(self, facade: LocalFacade) -> None:
        result = parsed(await resources.read_workflows(facade))
        assert {w["name"] for w in result["workflows"]} >= {"doubler"}

    async def test_one_workflow(self, facade: LocalFacade) -> None:
        result = parsed(await resources.read_workflow(facade, "doubler"))
        assert result["description"] == "Double the input"

    async def test_unknown_workflow(self, facade: LocalFacade) -> None:
        assert "error" in parsed(await resources.read_workflow(facade, "nope"))

    async def test_run_and_journal(self, facade: LocalFacade) -> None:
        run = parsed(await tools.run_workflow(facade, "doubler", "4"))
        assert parsed(await resources.read_run(facade, run["run_id"]))["output"] == 8

        journal = parsed(await resources.read_run_journal(facade, run["run_id"]))
        assert journal["journal"]

    async def test_unknown_run(self, facade: LocalFacade) -> None:
        assert "error" in parsed(await resources.read_run(facade, "nope"))
        assert "error" in parsed(await resources.read_run_journal(facade, "nope"))

    def test_every_declared_uri_has_a_reader(self) -> None:
        """The RESOURCES table is documentation; keep it honest."""
        assert set(resources.RESOURCES) == {
            "loom://workflows",
            "loom://workflows/{name}",
            "loom://runs/{run_id}",
            "loom://runs/{run_id}/journal",
        }


class TestPromptsUnit:
    def test_create_workflow_mentions_the_decorators(self) -> None:
        text = prompts.build_create_workflow_prompt("fetch and summarise")
        assert "fetch and summarise" in text
        assert "@workflow" in text and "@step" in text

    def test_debug_prompt_carries_status_and_journal(self) -> None:
        text = prompts.build_debug_run_prompt(
            {"status": "failed", "error": "boom"}, [{"step_id": "a"}]
        )
        assert "boom" in text and "step_id" in text

    def test_explain_handles_a_missing_workflow(self) -> None:
        assert "Not found" in prompts.build_explain_workflow_prompt("x", None)


# ---------------------------------------------------------------------------
# Integration — a real MCPServer instance
# ---------------------------------------------------------------------------


@pytest.fixture
def server(facade: LocalFacade):
    from loom.mcp_server import build_server

    return build_server(facade, name="loom-test")


class TestScheduler:
    """A parked timer must wake without anything external ticking.

    ``loom mcp`` is a long-running process; if it does not run the timer loop,
    ``ctx.sleep()`` parks forever and the workflow looks broken when it is
    merely unattended.
    """

    async def test_a_sleeping_run_resumes_under_the_server(self) -> None:
        import asyncio

        from loom.mcp_server import build_server

        @workflow(name="napper", description="Sleep, then finish")
        async def napper(ctx: Context, seconds: float) -> str:
            await ctx.sleep(seconds)
            return "woke"

        # threshold 0 so even a tiny sleep genuinely parks — otherwise the
        # engine holds a short wait in memory and the scheduler is never
        # involved, which is the thing under test.
        runtime = Runtime(store=MemoryStore(), inline_timer_threshold=0)
        runtime.register(napper)
        facade = LocalFacade(runtime)
        server = build_server(facade)

        async with server._lowlevel_server.lifespan(server._lowlevel_server):
            started = parsed(await tools.run_workflow(facade, "napper", "0.5"))
            assert started["status"] == "suspended"

            for _ in range(60):
                await asyncio.sleep(0.1)
                now = parsed(await tools.get_run_status(facade, started["run_id"]))
                if now["status"] != "suspended":
                    break

        assert now["status"] == "completed", "the timer never fired"
        assert now["output"] == "woke"

    async def test_it_can_be_turned_off(self, facade: LocalFacade) -> None:
        """One process per store should schedule; the rest opt out."""
        from loom.mcp_server import build_server

        build_server(facade, scheduler=False)
        assert facade.runtime._scheduler_task is None

    def test_a_remote_facade_gets_no_scheduler(self) -> None:
        """Its server schedules its own; this process has no Runtime to tick."""
        from loom.mcp_server.server import _scheduler_lifespan

        class Remote:
            """Stands in for RemoteFacade — no .runtime attribute."""

        assert _scheduler_lifespan(Remote()) is None


class TestServerRegistration:
    async def test_bind_address_reaches_the_transport(self, facade: LocalFacade) -> None:
        """The networked transports are unusable if these do not land.

        They were constructor arguments until mcp 2.0 moved them to the run
        call, so this asserts against `loom_transport` rather than the SDK's
        `settings`. The guarantee is unchanged and worth keeping pinned: what
        makes a dropped bind address dangerous is that nothing fails — the
        server starts, listens somewhere else, and the client simply never
        connects.
        """
        from loom.mcp_server import build_server

        built = build_server(facade, host="0.0.0.0", port=8931)

        assert (built.loom_transport.host, built.loom_transport.port) == ("0.0.0.0", 8931)
        assert built.loom_transport.run_kwargs() == {"host": "0.0.0.0", "port": 8931}

    async def test_transport_security_is_omitted_rather_than_none(
        self, facade: LocalFacade
    ) -> None:
        """Passing `transport_security=None` explicitly is not the same as not
        passing it: the SDK derives its own default for a loopback bind, and an
        explicit `None` would tell it there is none to apply."""
        from loom.mcp_server import build_server

        built = build_server(facade)

        assert "transport_security" not in built.loom_transport.run_kwargs()

    def test_the_cli_passes_host_and_port_through(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(
            ["mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "9001"]
        )
        assert (args.host, args.port) == ("0.0.0.0", 9001)

    def test_the_cli_has_a_no_authoring_flag(self) -> None:
        from loom.cli import build_parser

        args = build_parser().parse_args(["mcp", "--no-authoring"])
        assert args.no_authoring is True
        assert build_parser().parse_args(["mcp"]).no_authoring is False

    async def test_authoring_tools_absent_when_disabled(self, facade: LocalFacade) -> None:
        from loom.mcp_server import build_server
        from loom.mcp_server.authoring_config import AuthoringConfig

        server = build_server(facade, authoring=AuthoringConfig(enabled=False))
        names = {tool.name for tool in await server.list_tools()}
        # The run-management surface, which is everything an operator needs and
        # nothing that spends a model token.
        assert len(names) == 27
        assert "author_workflow" not in names
        assert "edit_workflow" not in names
        assert "save_workflow" not in names

    async def test_read_only_tools_marked_correctly(self, server) -> None:
        by_name = {tool.name: tool for tool in await server.list_tools()}
        for name in (
            "list_workflows",
            "get_run_status",
            "list_runs",
            "search_toolsets",
            "show_toolset",
            "get_tool_contract",
            "get_tool_docs",
            "validate_workflow_code",
        ):
            assert by_name[name].annotations.read_only_hint is True, name
        for name in ("run_workflow", "cancel_run", "save_workflow"):
            assert by_name[name].annotations.read_only_hint is False, name

    async def test_all_tools_are_registered(self, server) -> None:
        names = {tool.name for tool in await server.list_tools()}
        assert names == {
            "list_workflows",
            "run_workflow",
            "get_workflow_info",
            "schedule_workflow",
            "get_run_status",
            "list_runs",
            "get_run_journal",
            "get_run_progress",
            "approve_run",
            "send_event",
            "cancel_run",
            "retry_run",
            "replay_run",
            "search_toolsets",
            "show_toolset",
            "list_artifacts",
            "get_artifact_url",
            "put_artifact",
            # Closing the gap between what `RuntimeFacade` can do and what a
            # client could reach. Every one of these was a CLI-only capability,
            # so an MCP client could start a run and never answer the human
            # gate it parked on, or author a workflow and never change it.
            "list_pending",
            "respond_to_run",
            "pause_run",
            "unpause_run",
            "pin_run",
            "search_nodes",
            "show_node",
            "publish_workflow",
            "artifact_history",
            # Authoring tools — on by default; see test_mcp_authoring.py for
            # the coroutines themselves and the on/off gating.
            "get_tool_contract",
            "get_tool_docs",
            "call_read_operation",
            "validate_workflow_code",
            "smoke_test_workflow",
            "save_workflow",
            "author_workflow",
            "edit_workflow",
        }

    async def test_every_tool_has_annotations(self, server) -> None:
        """The SDK's hints — read_only / destructive / idempotent /
        open_world — that let a client reason about risk before calling a
        tool. Spelled camelCase through mcp 1.x; 2.0 made the field names
        snake_case and kept the old spellings as construction-time aliases,
        so writing them still works and *reading* one does not."""
        for tool in await server.list_tools():
            assert tool.annotations is not None, tool.name

    async def test_every_tool_is_described(self, server) -> None:
        """The description is what a model chooses on."""
        for tool in await server.list_tools():
            assert tool.description, tool.name

    async def test_schemas_come_from_the_signatures(self, server) -> None:
        """Hand-written schemas drift from the code; derived ones cannot."""
        by_name = {tool.name: tool for tool in await server.list_tools()}

        run = by_name["run_workflow"].input_schema
        assert set(run["properties"]) == {
            "workflow",
            "input_json",
            "idempotency_key",
        }
        assert run["required"] == ["workflow"]

        approve = by_name["approve_run"].input_schema
        assert approve["properties"]["approved"]["type"] == "boolean"

    async def test_resources_and_templates(self, server) -> None:
        direct = {str(r.uri) for r in await server.list_resources()}
        templates = {t.uri_template for t in await server.list_resource_templates()}

        assert direct == {"loom://workflows"}
        assert templates == {
            "loom://workflows/{name}",
            "loom://runs/{run_id}",
            "loom://runs/{run_id}/journal",
        }

    async def test_prompts_are_registered(self, server) -> None:
        names = {p.name for p in await server.list_prompts()}
        assert names == {
            "create_workflow",
            "debug_run",
            "explain_workflow",
            "optimize_workflow",
            "review_workflow",
        }

    async def test_instructions_warn_about_suspended(self, server) -> None:
        assert "suspended" in (server.instructions or "")

    async def test_instructions_describe_authoring_when_enabled(self, server) -> None:
        assert "save_workflow" in (server.instructions or "")


class TestValidateThenSmokeChain:
    """The loop a host model actually drives: write code, validate it, fix,
    then smoke-test — through the real MCP protocol layer, not the bare
    coroutines (see test_mcp_authoring.py for those)."""

    async def test_validate_then_smoke_on_the_same_code(self, server) -> None:
        code = (
            "from loom import Context, step, workflow\n\n"
            "@step\n"
            "async def double(n: int) -> int:\n"
            "    return n * 2\n\n"
            '@workflow(name="doubler2")\n'
            "async def doubler2(ctx: Context, n: int) -> int:\n"
            "    return await ctx.step(double, n)\n"
        )
        validated = json.loads(
            _text_of(await server.call_tool("validate_workflow_code", {"code": code}))
        )
        assert validated["valid"] is True

        smoked = json.loads(
            _text_of(
                await server.call_tool(
                    "smoke_test_workflow", {"code": code, "workflow_input_json": "4"}
                )
            )
        )
        assert smoked["ok"] is True


class TestServerCalls:
    async def test_calling_a_tool_reaches_the_facade(self, server) -> None:
        result = await server.call_tool("run_workflow", {"workflow": "doubler", "input_json": "6"})
        payload = json.loads(_text_of(result))
        assert payload["output"] == 12

    async def test_full_approval_round_trip_through_the_protocol(self, server) -> None:
        started = json.loads(
            _text_of(
                await server.call_tool(
                    "run_workflow", {"workflow": "approver", "input_json": '"x"'}
                )
            )
        )
        assert started["status"] == "suspended"

        approved = json.loads(
            _text_of(
                await server.call_tool(
                    "approve_run",
                    {"run_id": started["run_id"], "subject": "release"},
                )
            )
        )
        assert approved["status"] == "completed"

    async def test_reading_a_resource(self, server) -> None:
        contents = await server.read_resource("loom://workflows")
        payload = json.loads(next(iter(contents)).content)
        assert {w["name"] for w in payload["workflows"]} >= {"doubler"}

    async def test_reading_a_templated_resource(self, server) -> None:
        contents = await server.read_resource("loom://workflows/doubler")
        payload = json.loads(next(iter(contents)).content)
        assert payload["name"] == "doubler"

    async def test_getting_a_prompt(self, server) -> None:
        result = await server.get_prompt("create_workflow", {"description": "sync CRM"})
        assert "sync CRM" in str(result.messages[0].content)

    async def test_debug_prompt_fetches_the_run(self, server) -> None:
        started = json.loads(
            _text_of(
                await server.call_tool(
                    "run_workflow", {"workflow": "breaker", "input_json": '"x"'}
                )
            )
        )
        result = await server.get_prompt("debug_run", {"run_id": started["run_id"]})
        assert "upstream is down" in str(result.messages[0].content)


def _text_of(result) -> str:
    """The text payload of a tool result, across SDK return shapes.

    mcp 2.0 wraps what 1.x returned bare: ``call_tool`` hands back a
    ``CallToolResult`` carrying ``.content`` (and ``.structured_content``)
    rather than the content sequence itself.
    """
    blocks = getattr(result, "content", result)
    blocks = blocks[0] if isinstance(blocks, tuple) else blocks
    if isinstance(blocks, list | tuple):
        return blocks[0].text
    return str(blocks)


# ---------------------------------------------------------------------------
# End to end — a real subprocess over stdio
# ---------------------------------------------------------------------------


FLOWS = '''
"""Workflows for the MCP end-to-end test."""

from __future__ import annotations

from loom import Context, step, workflow


@step
async def score(email: str) -> int:
    """Score a lead."""
    return 90 if email.endswith(".gov") else 50


@workflow(name="onboard", description="Score a lead")
async def onboard(ctx: Context, email: str) -> int:
    """Score it."""
    return await ctx.step(score, email)


@workflow(name="refund", description="Refund pending approval")
async def refund(ctx: Context, amount: float) -> str:
    """Park on a human."""
    return f"refunded {amount}" if await ctx.wait_for_approval("refund") else "denied"
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "flows.py").write_text(FLOWS)
    return tmp_path


class TestStdioEndToEnd:
    """Drives the shipped entry point exactly as Claude Code would."""

    @staticmethod
    def _params(project: Path):
        from mcp import StdioServerParameters

        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "loom.cli", "mcp", "--module", "flows.py"],
            cwd=str(project),
            env={
                **os.environ,
                "LOOM_STORE": f"sqlite://{project / 'runs.db'}",
            },
        )

    async def test_handshake_and_tool_listing(self, project: Path) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params(project)) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            assert init.server_info.name == "loom"

            names = {t.name for t in (await session.list_tools()).tools}
            assert "run_workflow" in names
            assert "save_workflow" in names  # authoring tools on by default
            assert "author_workflow" in names  # …including the one-shot one
            assert len(names) == 35

    async def test_workflows_from_the_module_are_visible(self, project: Path) -> None:
        """The gap that made the original server useless: an empty registry."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params(project)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("list_workflows", {})
            payload = json.loads(result.content[0].text)

        assert {w["name"] for w in payload["workflows"]} == {"onboard", "refund"}

    async def test_run_and_inspect_over_the_wire(self, project: Path) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params(project)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            started = json.loads(
                (
                    await session.call_tool(
                        "run_workflow",
                        {"workflow": "onboard", "input_json": '"ada@nasa.gov"'},
                    )
                ).content[0].text
            )
            assert started["status"] == "completed"
            assert started["output"] == 90

            journal = json.loads(
                (
                    await session.call_tool(
                        "get_run_journal", {"run_id": started["run_id"]}
                    )
                ).content[0].text
            )
            assert [e["step_id"] for e in journal["journal"]] == ["score"]

    async def test_human_in_the_loop_over_the_wire(self, project: Path) -> None:
        """Suspend, then resolve the approval — all through the protocol."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params(project)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            started = json.loads(
                (
                    await session.call_tool(
                        "run_workflow",
                        {"workflow": "refund", "input_json": "42.5"},
                    )
                ).content[0].text
            )
            assert started["status"] == "suspended"
            assert "approve_run" in started["next_action"]

            approved = json.loads(
                (
                    await session.call_tool(
                        "approve_run",
                        {"run_id": started["run_id"], "subject": "refund"},
                    )
                ).content[0].text
            )

        assert approved["status"] == "completed"
        assert approved["output"] == "refunded 42.5"

    async def test_resources_and_prompts_over_the_wire(self, project: Path) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params(project)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            uris = {str(r.uri) for r in (await session.list_resources()).resources}
            assert "loom://workflows" in uris

            contents = await session.read_resource("loom://workflows")
            payload = json.loads(contents.contents[0].text)
            assert {w["name"] for w in payload["workflows"]} == {"onboard", "refund"}

            names = {p.name for p in (await session.list_prompts()).prompts}
            assert "debug_run" in names

    async def test_stdout_is_not_polluted_by_status_output(
        self, project: Path
    ) -> None:
        """stdio *is* the protocol channel — anything on stdout corrupts it.

        The handshake succeeding at all is the proof; this asserts the status
        line went to stderr where it belongs.
        """
        import subprocess

        done = subprocess.run(
            [sys.executable, "-m", "loom.cli", "mcp", "--module", "flows.py"],
            cwd=project,
            capture_output=True,
            text=True,
            input="",
            timeout=30,
            env={**os.environ, "LOOM_STORE": "memory://"},
        )
        assert "serving" in done.stderr
        assert "serving" not in done.stdout


# ---------------------------------------------------------------------------
# The deprecated shim
# ---------------------------------------------------------------------------


class TestDeprecatedBridge:
    def test_it_warns(self) -> None:
        from loom.mcp_server.bridge import RuntimeBridge

        with pytest.warns(DeprecationWarning, match="LocalFacade"):
            RuntimeBridge()

    async def test_it_delegates_to_the_shared_facade(self) -> None:
        """One port underneath, so a fix in the facade reaches both callers."""
        from loom.mcp_server.bridge import RuntimeBridge

        with pytest.warns(DeprecationWarning):
            bridge = RuntimeBridge()

        assert isinstance(bridge.facade, LocalFacade)

        bridge.register_workflow(doubler)
        run = await bridge.run_workflow("doubler", 4)
        assert run["status"] == "completed"
        # The shim keeps its historical key name.
        assert run["workflow_id"] == "doubler"
