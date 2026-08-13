"""CLI behaviour, driven through the real entry point.

Every test invokes ``python -m workflow_builder.cli`` in a subprocess against a
temporary project and a SQLite store, so what is exercised is exactly what a
user runs — argument parsing, target resolution, exit codes, and all.

Three things are asserted everywhere because they are the contract:

* the **exit code**, including ``3`` for suspended, which scripts branch on
* the **``--json`` payload**, which is what anything downstream consumes
* that failures produce a **message, not a traceback**
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from workflow_builder.cli.output import Exit

FLOWS = '''
"""Workflows for the CLI test project."""

from __future__ import annotations

from workflow_builder import Context, step, workflow


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
    """Double twice."""
    return await ctx.step(double, await ctx.step(double, n))


@workflow(name="approver", description="Waits for a human")
async def approver(ctx: Context, _input: str) -> str:
    """Park on an approval."""
    return "yes" if await ctx.wait_for_approval("release") else "no"


@workflow(name="waiter", description="Waits for an event")
async def waiter(ctx: Context, _input: str) -> str:
    """Park on a named event."""
    payload = await ctx.wait_for_event("go")
    return str(payload.get("token", ""))


@workflow(name="breaker", description="Always fails")
async def breaker(ctx: Context, _input: str) -> str:
    """Fail, to exercise retry and replay."""
    return await ctx.step(boom)
'''

PYPROJECT = """
[project]
name = "clitest"
version = "0.1.0"

[tool.loom]
modules = ["flows.py"]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A workflow project with a SQLite store, so runs persist across commands."""
    (tmp_path / "flows.py").write_text(FLOWS)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


def loom(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI the way a user does."""
    import os

    env = {**os.environ, "LOOM_STORE": f"sqlite://{project / 'runs.db'}"}
    return subprocess.run(
        [sys.executable, "-m", "workflow_builder.cli", *args],
        capture_output=True,
        text=True,
        cwd=project,
        env=env,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def payload(done: subprocess.CompletedProcess[str]):
    """The --json body, with a useful message when it is not valid JSON."""
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:  # pragma: no cover - only on failure
        pytest.fail(f"stdout was not JSON:\n{done.stdout}\n{done.stderr}")


def start_run(project: Path, workflow: str, *extra: str) -> dict:
    return payload(loom(project, "run", workflow, "--json", *extra))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_version(self, project: Path) -> None:
        from workflow_builder import __version__

        done = loom(project, "--version")
        assert done.returncode == Exit.OK
        assert __version__ in done.stdout

    def test_bare_invocation_lists_commands(self, project: Path) -> None:
        done = loom(project)
        assert done.returncode == Exit.OK
        for command in ("run", "runs", "show", "watch", "approve", "serve", "ui"):
            assert command in done.stdout

    def test_help_documents_the_exit_codes(self, project: Path) -> None:
        """Scripts branch on these, so they belong in --help."""
        done = loom(project, "--help")
        assert "3 suspended" in done.stdout

    def test_unknown_command_is_a_usage_error(self, project: Path) -> None:
        done = loom(project, "frobnicate")
        assert done.returncode != Exit.OK
        assert "Traceback" not in done.stderr


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestTargetResolution:
    def test_name_resolves_via_pyproject(self, project: Path) -> None:
        assert start_run(project, "doubler", "-i", "5")["output"] == 20

    def test_explicit_path_needs_no_config(self, project: Path) -> None:
        (project / "pyproject.toml").unlink()
        run = payload(loom(project, "run", "flows.py::doubler", "-i", "3", "--json"))
        assert run["output"] == 12

    def test_module_flag_overrides(self, project: Path) -> None:
        (project / "pyproject.toml").unlink()
        run = payload(
            loom(project, "run", "doubler", "-i", "2", "-m", "flows.py", "--json")
        )
        assert run["output"] == 8

    def test_unknown_workflow_names_the_known_ones(self, project: Path) -> None:
        done = loom(project, "run", "nope")
        assert done.returncode == Exit.USAGE
        assert "doubler" in done.stderr
        assert "Traceback" not in done.stderr

    def test_unimportable_module_reports_cleanly(self, project: Path) -> None:
        (project / "flows.py").write_text("import definitely_missing_pkg_xyz\n")
        done = loom(project, "run", "doubler")
        assert done.returncode == Exit.USAGE
        assert "Traceback" not in done.stderr


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


class TestInputParsing:
    def test_json_scalar(self, project: Path) -> None:
        assert start_run(project, "doubler", "-i", "7")["output"] == 28

    def test_bare_string_is_not_an_error(self, project: Path) -> None:
        """Most workflows take a string; demanding '"text"' would be hostile."""
        run = start_run(project, "approver", "-i", "just text")
        assert run["status"] == "suspended"

    def test_input_from_a_file(self, project: Path) -> None:
        (project / "in.json").write_text("11")
        assert start_run(project, "doubler", "-i", "@in.json")["output"] == 44

    def test_missing_input_file_is_a_usage_error(self, project: Path) -> None:
        done = loom(project, "run", "doubler", "-i", "@nope.json")
        assert done.returncode == Exit.USAGE
        assert "Traceback" not in done.stderr


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_completed_is_zero(self, project: Path) -> None:
        assert loom(project, "run", "doubler", "-i", "1").returncode == Exit.OK

    def test_failed_is_one(self, project: Path) -> None:
        assert loom(project, "run", "breaker", "-i", "x").returncode == Exit.FAILED

    def test_suspended_is_three(self, project: Path) -> None:
        """The reason this code exists: parked is neither success nor failure."""
        assert loom(project, "run", "approver", "-i", "x").returncode == Exit.SUSPENDED

    def test_usage_error_is_two(self, project: Path) -> None:
        assert loom(project, "show", "run_nope").returncode == Exit.USAGE


# ---------------------------------------------------------------------------
# Inspecting
# ---------------------------------------------------------------------------


class TestInspection:
    def test_runs_lists_what_ran(self, project: Path) -> None:
        start_run(project, "doubler", "-i", "1")
        start_run(project, "doubler", "-i", "2")

        rows = payload(loom(project, "runs", "--json"))
        assert len(rows) == 2
        assert {r["workflow"] for r in rows} == {"doubler"}

    def test_runs_filters_by_status(self, project: Path) -> None:
        start_run(project, "doubler", "-i", "1")
        start_run(project, "breaker", "-i", "x")

        failed = payload(loom(project, "runs", "--status", "failed", "--json"))
        assert [r["workflow"] for r in failed] == ["breaker"]

    def test_runs_filters_by_workflow(self, project: Path) -> None:
        start_run(project, "doubler", "-i", "1")
        start_run(project, "breaker", "-i", "x")

        rows = payload(loom(project, "runs", "-w", "doubler", "--json"))
        assert [r["workflow"] for r in rows] == ["doubler"]

    def test_runs_respects_limit(self, project: Path) -> None:
        for i in range(4):
            start_run(project, "doubler", "-i", str(i))
        assert len(payload(loom(project, "runs", "-n", "2", "--json"))) == 2

    def test_show_includes_the_journal(self, project: Path) -> None:
        run = start_run(project, "doubler", "-i", "5")
        detail = payload(loom(project, "show", run["run_id"], "--json"))

        assert detail["output"] == 20
        assert [e["step_id"] for e in detail["journal"]] == ["double", "double"]

    def test_show_reports_the_error_of_a_failed_run(self, project: Path) -> None:
        run = start_run(project, "breaker", "-i", "x")
        detail = payload(loom(project, "show", run["run_id"], "--json"))
        assert "upstream is down" in detail["error"]

    def test_watch_returns_immediately_for_a_settled_run(self, project: Path) -> None:
        run = start_run(project, "doubler", "-i", "1")
        done = loom(project, "watch", run["run_id"], "--json")
        assert done.returncode == Exit.OK

    def test_follow_streams_steps(self, project: Path) -> None:
        done = loom(project, "run", "doubler", "-i", "3", "--follow")
        assert done.returncode == Exit.OK
        assert "double" in done.stdout


# ---------------------------------------------------------------------------
# Acting on runs
# ---------------------------------------------------------------------------


class TestActions:
    def test_approve_completes_a_parked_run(self, project: Path) -> None:
        run = start_run(project, "approver", "-i", "x")
        assert run["status"] == "suspended"

        done = loom(project, "approve", run["run_id"], "release", "--json")
        assert done.returncode == Exit.OK
        assert payload(done)["output"] == "yes"

    def test_reject_takes_the_other_branch(self, project: Path) -> None:
        run = start_run(project, "approver", "-i", "x")
        result = payload(
            loom(project, "approve", run["run_id"], "release", "--reject", "--json")
        )
        assert result["output"] == "no"

    def test_send_delivers_an_arbitrary_event(self, project: Path) -> None:
        run = start_run(project, "waiter", "-i", "x")
        result = payload(
            loom(project, "send", run["run_id"], "go", '{"token": "abc"}', "--json")
        )
        assert result["output"] == "abc"

    def test_cancel_marks_a_parked_run_cancelled(self, project: Path) -> None:
        run = start_run(project, "approver", "-i", "x")
        done = loom(project, "cancel", run["run_id"], "--yes", "--json")
        assert payload(done)["status"] == "cancelled"
        assert done.returncode == Exit.CANCELLED

    def test_replay_reproduces_the_original(self, project: Path) -> None:
        run = start_run(project, "doubler", "-i", "6")
        replayed = payload(loom(project, "replay", run["run_id"], "--json"))

        assert replayed["status"] == "completed"
        assert replayed["output"] == run["output"]
        assert replayed["run_id"] != run["run_id"]

    def test_retry_reruns_a_failure(self, project: Path) -> None:
        run = start_run(project, "breaker", "-i", "x")
        retried = payload(loom(project, "retry", run["run_id"], "--json"))
        # The step always raises, so it fails again — the point is that retry
        # ran and reported, rather than erroring on the way in.
        assert retried["run_id"] == run["run_id"]
        assert retried["status"] == "failed"

    def test_acting_on_an_unknown_run_is_a_usage_error(self, project: Path) -> None:
        for action in ("cancel", "retry", "replay"):
            done = loom(project, action, "run_nope", "--yes")
            assert done.returncode == Exit.USAGE, action
            assert "Traceback" not in done.stderr


# ---------------------------------------------------------------------------
# Workflows and publishing
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_workflows_lists_what_is_importable(self, project: Path) -> None:
        entries = payload(loom(project, "workflows", "--json"))
        names = {e["name"] for e in entries}

        assert {"doubler", "approver", "breaker", "waiter"} <= names
        assert all(e["executable"] for e in entries)

    def test_publish_records_the_code_hash(self, project: Path) -> None:
        record = payload(loom(project, "publish", "doubler", "--json"))

        assert record["name"] == "doubler"
        assert record["code_hash"]
        assert record["source_file"].endswith("flows.py")

    def test_a_published_workflow_survives_the_process(self, project: Path) -> None:
        loom(project, "publish", "doubler", "--json")
        entries = payload(loom(project, "workflows", "--json"))
        assert any(e["name"] == "doubler" for e in entries)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


class TestOutputContract:
    def test_json_mode_emits_only_json(self, project: Path) -> None:
        """Anything else on stdout breaks `| jq`."""
        done = loom(project, "run", "doubler", "-i", "2", "--json")
        json.loads(done.stdout)

    def test_errors_go_to_stderr_so_stdout_stays_clean(self, project: Path) -> None:
        done = loom(project, "run", "nope", "--json")
        assert done.stdout.strip() == ""
        assert done.stderr.strip()

    def test_human_mode_has_no_escape_codes_when_piped(self, project: Path) -> None:
        """subprocess capture is not a TTY, so styling must be stripped."""
        done = loom(project, "runs")
        assert "\x1b[" not in done.stdout

    def test_human_mode_leaves_no_markup_tags(self, project: Path) -> None:
        run = start_run(project, "approver", "-i", "x")
        done = loom(project, "show", run["run_id"])
        assert "[yellow]" not in done.stdout
        assert "[/dim]" not in done.stdout


# ---------------------------------------------------------------------------
# Authoring commands still work
# ---------------------------------------------------------------------------


class TestAuthoringCommands:
    def test_check_writes_the_artifacts(self, project: Path) -> None:
        done = loom(project, "check", "flows.py")
        assert done.returncode == Exit.OK
        assert (project / "flows.graph.json").exists()
        assert (project / "flows.description.md").exists()

    def test_check_json_reports_the_graph(self, project: Path) -> None:
        report = payload(loom(project, "check", "flows.py", "--json"))
        assert report["nodes"] > 0

    def test_graph_renders_mermaid(self, project: Path) -> None:
        done = loom(project, "graph", "flows.py")
        assert "flowchart" in done.stdout

    def test_graph_renders_react_flow(self, project: Path) -> None:
        done = loom(project, "graph", "flows.py", "--format", "react-flow")
        assert "nodes" in json.loads(done.stdout)

    def test_init_scaffolds(self, project: Path) -> None:
        done = loom(project, "init", "sub")
        assert done.returncode == Exit.OK
        assert (project / "sub" / "workflows" / "quickstart.py").exists()

    def test_missing_file_is_a_message_not_a_traceback(self, project: Path) -> None:
        done = loom(project, "check", "nope.py")
        assert done.returncode == Exit.USAGE
        assert "Traceback" not in done.stderr


# ---------------------------------------------------------------------------
# Remote mode
# ---------------------------------------------------------------------------


class TestRemoteMode:
    """The same commands, against a server, importing nothing."""

    @pytest.fixture
    def backend(self):
        pytest.importorskip("fastapi")
        pytest.importorskip("httpx")

        import httpx

        from workflow_builder import Context, Runtime, step, workflow
        from workflow_builder.cli.targets import RemoteBackend
        from workflow_builder.server.app import create_app
        from workflow_builder.server.client import LoomClient
        from workflow_builder.state.memory import MemoryStore

        @step
        async def triple(n: int) -> int:
            """Triple it."""
            return n * 3

        @workflow(name="tripler")
        async def tripler(ctx: Context, n: int) -> int:
            return await ctx.step(triple, n)

        rt = Runtime(store=MemoryStore())
        rt.register(tripler)
        transport = httpx.ASGITransport(app=create_app(rt))
        client = LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        )
        return RemoteBackend(client)

    async def test_workflows(self, backend) -> None:
        names = {w["name"] for w in await backend.workflows()}
        assert "tripler" in names

    async def test_start_and_get(self, backend) -> None:
        run = await backend.start("tripler", 5)
        assert run["output"] == 15
        assert (await backend.get(run["run_id"]))["run_id"] == run["run_id"]

    async def test_unknown_run_is_none_not_an_error(self, backend) -> None:
        assert await backend.get("run_nope") is None

    async def test_journal(self, backend) -> None:
        run = await backend.start("tripler", 2)
        assert [e["step_id"] for e in await backend.journal(run["run_id"])] == ["triple"]

    async def test_replay_over_http(self, backend) -> None:
        run = await backend.start("tripler", 4)
        assert (await backend.replay(run["run_id"]))["status"] == "completed"

    async def test_retry_route_exists(self, backend) -> None:
        """Runtime.retry() had no HTTP equivalent until the CLI needed one."""
        run = await backend.start("tripler", 4)
        assert (await backend.retry(run["run_id"]))["status"] == "completed"

    async def test_publishing_remotely_is_refused_with_a_reason(self, backend) -> None:
        from workflow_builder.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match="where the code is"):
            await backend.publish("tripler")


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------


class TestTui:
    @pytest.fixture
    def backend(self):
        """A backend over a Runtime with one completed and one parked run."""
        from workflow_builder import Context, Runtime, step, workflow
        from workflow_builder.cli.targets import LocalBackend
        from workflow_builder.state.memory import MemoryStore

        @step
        async def echo(value: str) -> str:
            """Echo it."""
            return value

        @workflow(name="quick")
        async def quick(ctx: Context, value: str) -> str:
            return await ctx.step(echo, value)

        @workflow(name="parked")
        async def parked(ctx: Context, _input: str) -> str:
            return "ok" if await ctx.wait_for_approval("go") else "no"

        rt = Runtime(store=MemoryStore())
        rt.register_all([quick, parked])
        return LocalBackend(rt)

    async def test_the_app_starts_and_lists_runs(self, backend) -> None:
        pytest.importorskip("textual")
        from workflow_builder.cli.tui import LoomApp

        await backend.start("quick", "hello")
        await backend.start("parked", "x")

        app = LoomApp(backend)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#runs")
            assert table.row_count == 2

    async def test_selecting_a_run_shows_its_journal(self, backend) -> None:
        pytest.importorskip("textual")
        from workflow_builder.cli.tui import LoomApp

        await backend.start("quick", "hello")

        app = LoomApp(backend)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#journal").row_count >= 1

    async def test_approving_from_the_ui_completes_the_run(self, backend) -> None:
        """The pane that has no non-interactive equivalent."""
        pytest.importorskip("textual")
        from workflow_builder.cli.tui import LoomApp

        run = await backend.start("parked", "x")
        assert run["status"] == "suspended"

        app = LoomApp(backend)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause(0.2)

        assert (await backend.get(run["run_id"]))["status"] == "completed"

    async def test_quit_binding_exists(self, backend) -> None:
        pytest.importorskip("textual")
        from workflow_builder.cli.tui import LoomApp

        assert any(b.key == "q" for b in LoomApp.BINDINGS)


class TestSiblingImports:
    """A workflow file that imports a sibling must load.

    One file is where a project starts, not where it stays. Without the file's
    own directory on the path this fails with ModuleNotFoundError, which reads
    as a broken workflow rather than a missing search path.
    """

    def test_a_workflow_file_can_import_its_neighbour(self, tmp_path: Path) -> None:
        from workflow_builder.cli.targets import collect_workflows, load_module

        (tmp_path / "helper.py").write_text("GREETING = 'hi'\n")
        (tmp_path / "flow.py").write_text(
            "from workflow_builder import Context, workflow\n"
            "import helper\n"
            "@workflow(name='greeter')\n"
            "async def greeter(ctx: Context, _in: str) -> str:\n"
            "    return helper.GREETING\n"
        )

        module = load_module(str(tmp_path / "flow.py"))
        assert [w.name for w in collect_workflows(module)] == ["greeter"]
