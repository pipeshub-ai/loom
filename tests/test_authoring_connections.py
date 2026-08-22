"""Connecting an account from inside an authoring run.

Three gaps, all the same shape as the one that started this: a capability that
existed and nothing wired to it.

* `credential_store_scope` was bound at exactly one site — the engine's step
  attempt loop — so `loom connect jira` stored a credential the *runtime* could
  use and the authoring agent could not. Rung 2 of the resolution ladder
  ("resolve now, bake the id in") was unreachable for every connected toolset.
* A missing credential surfaced as an ordinary exception, which reads as "this
  toolset is broken" — and the cheapest repair a model can find for a broken
  toolset is to stop importing it.
* Nothing could obtain one mid-run, so the only move after a failed lookup was
  to give up on the value.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from loom.agents.coding_tools import (
    ConnectGate,
    _call_read_operation,
    build_coding_tools,
    make_connect_tool,
)
from loom.connectors.credentials import MemoryCredentialStore, StoredCredential
from loom.core.secret import Secret
from loom.runtime.engine import Runtime


@pytest.fixture(autouse=True)
def _jira_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """These assert what happens when nothing is connected, so nothing is.

    Two leaks to close, and both are ordering dependencies rather than noise:
    a `JIRA_*` variable in the ambient environment, and the module-level
    `_default_client` singleton that another test constructed with a fake
    transport and left set. Without this the file passes alone and fails in a
    full run, which is the least useful shape a test can have.
    """
    import loom.toolsets.jira.client as jira_client

    for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jira_client, "_default_client", None, raising=False)


@pytest.fixture
def runtime() -> Runtime:
    return Runtime()


class TestTheStoreIsBoundDuringAuthoring:
    """The defect: `loom connect jira` and the agent disagreed about what was
    connected, because only one of them was inside a `credential_store_scope`."""

    async def test_the_bound_store_is_the_one_a_read_sees(
        self, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.connectors.credentials import current_credential_store

        store = MemoryCredentialStore()
        await store.put("jira", StoredCredential(token=Secret("tok")))

        seen: list[Any] = []

        async def spy(**kwargs: Any) -> list[Any]:
            seen.append(current_credential_store())
            return []

        import loom.toolsets.jira.tools as jira_tools

        monkeypatch.setattr(jira_tools, "jira_resolve_project", spy)
        await _call_read_operation(
            "jira.projects.resolve",
            {"project_name": "saas"},
            registry=runtime.toolsets,
            credentials=store,
        )
        assert seen == [store], "the authoring read ran outside the store's scope"

    async def test_no_store_is_exactly_what_shipped_before(
        self, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.connectors.credentials import current_credential_store

        seen: list[Any] = []

        async def spy(**kwargs: Any) -> list[Any]:
            seen.append(current_credential_store())
            return []

        import loom.toolsets.jira.tools as jira_tools

        monkeypatch.setattr(jira_tools, "jira_resolve_project", spy)
        await _call_read_operation(
            "jira.projects.resolve", {"project_name": "x"}, registry=runtime.toolsets
        )
        assert seen == [None]

    def test_the_facade_passes_its_runtime_store(self) -> None:
        """The wiring, not the unit. `interaction.py` shipped complete and
        fully unit-tested, and no caller passed it in."""
        import inspect

        from loom.facade import LocalFacade

        source = inspect.getsource(LocalFacade._coding_agent)
        assert 'credentials=getattr(self.runtime, "credentials", None)' in source


class TestNotConnectedIsAStateNotAFailure:
    async def test_it_names_the_toolset_and_what_would_fix_it(
        self, runtime: Runtime
    ) -> None:
        result = json.loads(
            await _call_read_operation(
                "jira.projects.resolve", {"project_name": "saas"},
                registry=runtime.toolsets,
            )
        )
        assert result["error"] == "not_connected"
        assert result["toolset"] == "jira"
        assert result["needs"]["provider"] == "atlassian"
        assert result["needs"]["credential"] == "jira"
        assert result["next"] == 'connect_toolset("jira")'
        assert result["setup_url"].startswith("https://developer.atlassian.com")

    async def test_it_tells_the_model_not_to_change_the_workflow(
        self, runtime: Runtime
    ) -> None:
        """The instruction travels with the finding.

        In the system prompt it would be paid for on every turn of every job
        and read on the one where it matters — the reason `node_contract`'s
        worked example was removed from the prompt too.
        """
        result = json.loads(
            await _call_read_operation(
                "jira.projects.resolve", {"project_name": "x"},
                registry=runtime.toolsets,
            )
        )
        assert "Never drop the integration" in result["note"]
        assert "your code are" in result["note"]

    async def test_a_real_failure_is_still_a_real_failure(
        self, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Widening the catch must not swallow a genuine error.

        A stage that reports a working thing as broken is bad; one that reports
        a broken thing as merely unconfigured is worse, because nobody looks.
        """

        async def boom(**kwargs: Any) -> list[Any]:
            raise TimeoutError("the gateway did not answer")

        import loom.toolsets.jira.tools as jira_tools

        monkeypatch.setattr(jira_tools, "jira_resolve_project", boom)
        result = json.loads(
            await _call_read_operation(
                "jira.projects.resolve", {"project_name": "x"},
                registry=runtime.toolsets,
            )
        )
        assert result["error"] != "not_connected"
        assert "TimeoutError" in result["error"]


class TestTheConnectTool:
    """Offered only when something can actually connect."""

    def test_it_is_absent_with_no_flow(self, runtime: Runtime) -> None:
        names = {t.name for t in build_coding_tools(registry=runtime.toolsets)}
        assert "connect_toolset" not in names

    def test_it_is_present_when_one_is_composed(self, runtime: Runtime) -> None:
        async def connect(toolset_id: str) -> dict[str, Any]:
            return {"connected": True, "toolset": toolset_id}

        names = {
            t.name
            for t in build_coding_tools(registry=runtime.toolsets, connect=connect)
        }
        assert "connect_toolset" in names

    async def test_it_returns_what_the_flow_answered(self) -> None:
        async def connect(toolset_id: str) -> dict[str, Any]:
            return {"connected": True, "toolset": toolset_id, "scopes": ["read"]}

        tool = make_connect_tool(connect)
        assert json.loads(await tool.fn("jira"))["connected"] is True

    async def test_a_failure_is_a_payload_never_a_raise(self) -> None:
        """A raise aborts the model's turn and it learns nothing."""

        async def connect(toolset_id: str) -> dict[str, Any]:
            raise RuntimeError("the browser never came back")

        result = json.loads(await make_connect_tool(connect).fn("jira"))
        assert "RuntimeError" in result["error"]

    async def test_the_budget_is_counted_in_attempts(self) -> None:
        """A model that cannot make a lookup work tries connecting again rather
        than concluding it cannot resolve the value here."""

        async def connect(toolset_id: str) -> dict[str, Any]:
            return {"connected": False}

        gate = ConnectGate(budget=2)
        tool = make_connect_tool(connect, gate=gate)
        await tool.fn("jira")
        await tool.fn("jira")
        third = json.loads(await tool.fn("jira"))
        assert "already tried" in third["error"]
        assert "resolved at run time" in third["note"]

    async def test_it_is_off_during_repair_and_smoke(self) -> None:
        """So a model cannot deadlock CI by opening a browser nobody is at."""

        async def connect(toolset_id: str) -> dict[str, Any]:
            return {"connected": True}

        gate = ConnectGate(enabled=False)
        result = json.loads(await make_connect_tool(connect, gate=gate).fn("jira"))
        assert "off during this phase" in result["error"]

    def test_the_agent_closes_the_gate_before_repair(self) -> None:
        import inspect

        from loom.agents.coding_agent import WorkflowCodingAgent

        source = inspect.getsource(WorkflowCodingAgent._generate)
        assert "self._connect_gate.enabled = False" in source


class TestTheConnectionStage:
    """A warning, and never an error — in both directions."""

    async def _run(self, code: str, runtime: Runtime, credentials: Any = None) -> Any:
        from loom.agents.checks import CheckContext
        from loom.agents.coding_agent import _toolset_modules
        from loom.agents.stages import ConnectionStage

        context = CheckContext(
            spec="", toolset_modules=_toolset_modules(runtime.toolsets)
        )
        return await ConnectionStage(runtime.toolsets, credentials).run(code, context)

    IMPORTS_JIRA = (
        "from loom import Context, workflow\n"
        "from loom.toolsets.jira.tools import jira_search_issues\n"
    )

    async def test_an_unconnected_import_warns(self, runtime: Runtime) -> None:
        result = await self._run(self.IMPORTS_JIRA, runtime)
        assert result.issues
        assert all(issue.severity == "warning" for issue in result.issues)
        assert "loom connect jira" in result.issues[0].message

    async def test_it_never_errors(self, runtime: Runtime) -> None:
        """The repair loop reads `report.errors`, and unchanged code ends the
        repair — so an error here asks a model to fix a *machine*, and the
        cheapest fix available is to stop importing the toolset."""
        result = await self._run(self.IMPORTS_JIRA, runtime)
        assert not [i for i in result.issues if i.severity == "error"]

    async def test_a_connected_toolset_says_nothing(self, runtime: Runtime) -> None:
        """A stage that fires on correct code is worse than no stage."""
        store = MemoryCredentialStore()
        await store.put("jira", StoredCredential(token=Secret("tok")))
        result = await self._run(self.IMPORTS_JIRA, runtime, store)
        assert result.issues == []

    async def test_a_file_importing_no_toolset_says_nothing(
        self, runtime: Runtime
    ) -> None:
        result = await self._run("from loom import workflow\n", runtime)
        assert result.issues == []

    async def test_it_matches_modules_not_ids(self, runtime: Runtime) -> None:
        """`google_calendar` lives at `loom.toolsets.google.calendar`, so an
        id-based match would miss every nested toolset."""
        code = "from loom.toolsets.google.calendar.tools import calendar_list_events\n"
        result = await self._run(code, runtime)
        assert result.issues
        assert "google_calendar" in result.issues[0].message

    def test_it_is_in_the_default_pipeline_before_the_expensive_stages(self) -> None:
        from loom.agents.stages import ConnectionStage, default_stages

        stages = default_stages(smoke=False)
        names = [s.name for s in stages]
        assert "connections" in names
        connections = next(s for s in stages if isinstance(s, ConnectionStage))
        assert connections.blocking is False
        assert connections.cost < 20, "it reads manifests; it must be cheap"


class TestFindOperationAcceptsWhatTheDocsTeach:
    """`describe` renders "Resolve a project with jira_resolve_project" — the
    *function* name, because that is what generated code writes — while this
    matched only the operation id."""

    def test_both_spellings_resolve(self) -> None:
        from loom.toolsets.registry import builtin_catalog

        manifest = builtin_catalog().get("jira")
        assert manifest.find_operation("projects.resolve") is not None
        assert manifest.find_operation("jira_resolve_project") is not None
        assert manifest.find_operation("projects.resolve") is manifest.find_operation(
            "jira_resolve_project"
        )

    def test_an_unknown_name_is_still_unknown(self) -> None:
        from loom.toolsets.registry import builtin_catalog

        assert builtin_catalog().get("jira").find_operation("nope") is None

    async def test_the_tool_accepts_the_name_it_was_given(
        self, runtime: Runtime
    ) -> None:
        """The whole point: the model calls what the docs told it to call."""
        result = json.loads(
            await _call_read_operation(
                "jira.jira_resolve_project", {"project_name": "saas"},
                registry=runtime.toolsets,
            )
        )
        assert "no operation" not in json.dumps(result)


class TestThePreflight:
    """A toolset call checks it can authenticate before it makes it.

    Without this a generated workflow calling Jira with nothing connected
    failed with `ValueError: JIRA_URL is required (env var or base_url
    argument)` — an environment variable name, raised deep inside the client,
    naming neither the toolset nor the fix, and only after everything before it
    had already run.
    """

    async def test_a_missing_credential_names_the_toolset_and_the_fix(
        self, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.connectors.inspect import preflight

        for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        reason = await preflight("jira_search_issues", toolsets=runtime.toolsets)

        assert "jira is not connected" in reason
        assert "atlassian" in reason
        assert "loom connect jira" in reason

    async def test_a_local_step_is_never_judged(self, runtime: Runtime) -> None:
        """Nothing declares what it needs, and inventing a requirement would
        guess at the declaration a manifest exists to make."""
        from loom.connectors.inspect import preflight

        assert await preflight("my_own_helper", toolsets=runtime.toolsets) == ""

    async def test_a_toolset_needing_nothing_is_never_judged(
        self, runtime: Runtime
    ) -> None:
        from loom.connectors.inspect import preflight

        assert await preflight("duckduckgo_search", toolsets=runtime.toolsets) == ""

    async def test_the_environment_satisfying_it_is_enough(
        self, runtime: Runtime
    ) -> None:
        """The commonest configuration, and the one a false refusal would
        break: no stored credential, everything in `.env`."""
        from loom.connectors.inspect import preflight

        assert await preflight(
            "jira_search_issues",
            toolsets=runtime.toolsets,
            environ={"JIRA_URL": "u", "JIRA_EMAIL": "e", "JIRA_API_TOKEN": "t"},
        ) == ""

    async def test_a_stored_credential_is_enough(self, runtime: Runtime) -> None:
        from loom.connectors.inspect import preflight

        store = MemoryCredentialStore()
        await store.put("jira", StoredCredential(token=Secret("tok")))
        assert await preflight(
            "jira_search_issues",
            toolsets=runtime.toolsets,
            credentials=store,
            environ={},
        ) == ""

    async def test_a_store_it_cannot_inspect_is_never_judged(
        self, runtime: Runtime
    ) -> None:
        """A host with its own store shape is invisible to this, and a
        preflight that refuses a working deployment is worse than one that
        misses."""
        from loom.connectors.inspect import preflight

        class Opaque:
            async def get(self, name: str) -> Any:
                return Secret("tok")

        assert await preflight(
            "jira_search_issues",
            toolsets=runtime.toolsets,
            credentials=Opaque(),
            environ={},
        ) == ""


class TestTheFailureIsExplainedInARun:
    async def test_it_fails_once_rather_than_retrying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A credential does not appear between two attempts.

        The check sits above the retry loop because it is a property of the
        *call*: inside it, a three-attempt policy printed the same impossible
        failure three times.
        """
        from loom import Context, workflow
        from loom.stores import MemoryStore
        from loom.toolsets.jira.tools import jira_search_issues

        for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)

        @workflow(name="preflight_probe")
        async def probe(ctx: Context, _: None = None) -> Any:
            return await ctx.step(jira_search_issues, "project = PA")

        rt = Runtime(store=MemoryStore())
        rt.register(probe)
        result = await rt.run(probe)

        assert result.status.value == "failed"
        assert result.error is not None
        # Not RetriesExhausted, which is what three attempts would have left:
        # `MissingCredentials` is a `NonRetryableError`, because a credential
        # does not appear between two attempts.
        assert result.error.type == "MissingCredentials"
        assert "loom connect jira" in result.error.message

    async def test_the_dial_turns_it_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For a host that would rather see its client's own complaint."""
        from loom import Context, workflow
        from loom.stores import MemoryStore
        from loom.toolsets.jira.tools import jira_search_issues

        for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)

        @workflow(name="preflight_off")
        async def probe(ctx: Context, _: None = None) -> Any:
            return await ctx.step(jira_search_issues, "project = PA")

        rt = Runtime(store=MemoryStore(), explain_credentials=False)
        rt.register(probe)
        result = await rt.run(probe)

        assert result.error is not None
        # Straight through to the client's own complaint, exactly as before.
        assert result.error.type != "MissingCredentials"

    async def test_a_local_step_runs_untouched(self) -> None:
        from loom import Context, step, workflow
        from loom.stores import MemoryStore

        @step
        async def add(a: int, b: int) -> int:
            return a + b

        @workflow(name="preflight_local")
        async def probe(ctx: Context, _: None = None) -> int:
            return await ctx.step(add, 2, 3)

        rt = Runtime(store=MemoryStore())
        rt.register(probe)
        assert (await rt.run(probe)).output == 5
