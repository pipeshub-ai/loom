"""Phase 0 — containment.

Each class here pins one defect that a 6,000-test suite did not catch, and the
reason it did not catch it is recorded beside the test. The pattern in every
case was the same: the suite asserted a *table* (a role's permissions, a
policy's fields) or drove a *serial* path, and the enforcement point went
untested. So these drive the enforcement point.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from loom import Context, Runtime, step, workflow
from loom.core.exceptions import ContinueAsNew  # noqa: F401  (import smoke)
from loom.runtime.effects import EffectCall, GuardedBroker
from loom.security.authority import Authority
from loom.security.rbac import Permission, Role, permission_of
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# C3 — the call ceiling under concurrency
# ---------------------------------------------------------------------------


@step
async def _slow(i: int) -> int:
    await asyncio.sleep(0.01)
    return i


@workflow(name="p0_race")
async def _race(ctx: Context, _inp: object = None) -> int:
    return sum(await ctx.gather(*[ctx.step(_slow, i) for i in range(10)]))


class TestCallCeilingHoldsUnderConcurrency:
    """`max_calls` counted completions, so it bounded nothing in parallel.

    Every prior broker test dispatched serially, where check-then-increment and
    reserve-then-perform are indistinguishable. `ctx.gather` is the documented
    parallel primitive and the shape a runaway agent loop actually takes, so
    the one case the ceiling exists for was the one case it did not cover.
    """

    @pytest.mark.asyncio
    async def test_gather_cannot_exceed_the_ceiling(self) -> None:
        broker = GuardedBroker(max_calls=3)
        runtime = Runtime(store=MemoryStore(), broker=broker, authority=Authority())
        runtime.register(_race)

        await runtime.run(_race, None)

        assert broker.dispatched == 3

    @pytest.mark.asyncio
    async def test_refusals_do_not_consume_budget(self) -> None:
        broker = GuardedBroker(max_calls=1)
        authority = Authority(dry_run=True)

        async def perform() -> str:
            return "done"

        write = EffectCall(kind="step", target="w", perform=perform)
        first = await broker.dispatch(write, authority)

        assert not first.ok
        assert broker.dispatched == 0

    @pytest.mark.asyncio
    async def test_the_ceiling_is_reserved_before_the_await_not_after(self) -> None:
        """Ten concurrent dispatches, one slot: exactly one may run."""
        broker = GuardedBroker(max_calls=1)
        started = 0

        async def perform() -> str:
            nonlocal started
            started += 1
            await asyncio.sleep(0.01)
            return "done"

        calls = [
            EffectCall(kind="step", target=f"s{i}", perform=perform) for i in range(10)
        ]
        results = await asyncio.gather(
            *(broker.dispatch(call, Authority()) for call in calls)
        )

        assert started == 1
        assert sum(1 for r in results if r.ok) == 1

    @pytest.mark.asyncio
    async def test_an_unbounded_broker_still_dispatches_everything(self) -> None:
        broker = GuardedBroker()
        runtime = Runtime(store=MemoryStore(), broker=broker, authority=Authority())
        runtime.register(_race)

        result = await runtime.run(_race, None)

        assert result.output == sum(range(10))
        assert broker.dispatched == 10


# ---------------------------------------------------------------------------
# C4 — every mutating Runtime operation is guarded
# ---------------------------------------------------------------------------


#: Public `Runtime` coroutines that read, coordinate, or are the scheduler's own
#: loop, and so carry no permission of their own. Anything *not* here must
#: declare one — the list is the exemption, so a method added tomorrow fails
#: this test by default rather than silently joining `retry` and `approve` in
#: being unguarded.
_UNGUARDED_BY_DESIGN = frozenset({
    "close",
    "persist_journal",
    "provenance",
    "published",
    "reclaim_orphans",
    "register",
    "register_all",
    "resolve_workflow",
    "shutdown",
    "start_scheduler",
    "supervise",
    "take_event",
    "tick",
    "unsupervise",
    "version_of",
    "wait",
    "observe_run",
    "limiter_for",
    "is_cancellation_requested",
    "require_artifacts",
    "require_staging",
    "require_signed_urls",
    "from_env",
    "workflows",
})


def _public_coroutines() -> dict[str, object]:
    import inspect

    found = {}
    for name in dir(Runtime):
        if name.startswith("_"):
            continue
        attribute = inspect.getattr_static(Runtime, name)
        target = getattr(attribute, "__func__", attribute)
        if inspect.iscoroutinefunction(target):
            found[name] = target
    return found


class TestEveryMutatingRuntimeMethodIsGuarded:
    """`Permission.GRANT_APPROVE` was in the enum, mapped to a role, and
    asserted by a test — that only ever read the role table. Four mutating
    methods had no check at all. A table test cannot find that; enumerating the
    methods can."""

    def test_every_public_coroutine_is_guarded_or_exempt(self) -> None:
        unguarded = sorted(
            name
            for name, fn in _public_coroutines().items()
            if permission_of(fn) is None and name not in _UNGUARDED_BY_DESIGN
        )

        assert unguarded == [], (
            f"these Runtime coroutines declare no Permission: {unguarded}. "
            "Add @requires(...), or add the name to _UNGUARDED_BY_DESIGN with "
            "a reason."
        )

    @pytest.mark.parametrize(
        ("method", "permission"),
        [
            ("run", Permission.FLOW_RUN),
            ("submit", Permission.FLOW_RUN),
            ("resume", Permission.FLOW_RUN),
            ("retry", Permission.FLOW_RUN),
            ("replay", Permission.RUN_REPLAY),
            ("cancel", Permission.FLOW_CANCEL),
            ("approve", Permission.FLOW_RUN),
            ("send_event", Permission.FLOW_RUN),
            ("publish", Permission.FLOW_DEPLOY),
        ],
    )
    def test_the_permission_is_the_one_intended(
        self, method: str, permission: Permission
    ) -> None:
        assert permission_of(_public_coroutines()[method]) is permission


@workflow(name="p0_trivial")
async def _trivial(ctx: Context, _inp: object = None) -> str:
    return "done"


class TestViewerCannotMutate:
    """The escalation itself: a read-only role could re-execute a run against
    live credentials, and could answer a human approval — which is the act that
    clears taint and re-permits writes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("call", "args"),
        [
            ("retry", ("run-1",)),
            ("approve", ("run-1", "refund")),
            ("send_event", ("run-1", "go")),
            ("publish", ("p0_trivial",)),
            ("replay", ("run-1",)),
            ("cancel", ("run-1",)),
        ],
    )
    async def test_viewer_is_refused(self, call: str, args: tuple) -> None:
        from loom.security.rbac import AuthorizationError

        runtime = Runtime(store=MemoryStore(), role=Role.VIEWER)
        runtime.register(_trivial)

        with pytest.raises(AuthorizationError):
            await getattr(runtime, call)(*args)

    @pytest.mark.asyncio
    async def test_an_operator_may_still_approve(self) -> None:
        """Answering a human gate is the operator's job, so the fix must not
        make the role that does it unable to."""
        runtime = Runtime(store=MemoryStore(), role=Role.OPERATOR)
        runtime.register(_trivial)
        result = await runtime.run(_trivial, None)

        await runtime.approve(result.run_id, "anything")  # does not raise

    @pytest.mark.asyncio
    async def test_no_role_configured_enforces_nothing(self) -> None:
        runtime = Runtime(store=MemoryStore())
        runtime.register(_trivial)

        assert (await runtime.run(_trivial, None)).output == "done"


# ---------------------------------------------------------------------------
# C5 — a sandbox refuses what it cannot enforce
# ---------------------------------------------------------------------------


class TestSandboxRefusesUnenforceablePolicy:
    """`SandboxPolicy.network` documented the rule this violated: "a sandbox
    that cannot actually prevent egress must say so through `enforces` rather
    than accept the flag and ignore it"."""

    def test_a_bare_policy_asks_for_no_network_guarantee(self) -> None:
        from loom.runtime.sandbox import NetworkPolicy, SandboxPolicy

        assert SandboxPolicy().network is NetworkPolicy.UNSPECIFIED

    def test_bool_false_still_means_deny(self) -> None:
        """Backwards compatibility with the old two-valued field, without
        letting the *default* start refusing every existing construction."""
        from loom.runtime.sandbox import NetworkPolicy, SandboxPolicy

        assert SandboxPolicy(network=False).network is NetworkPolicy.DENY
        assert SandboxPolicy(network=True).network is NetworkPolicy.ALLOW

    @pytest.mark.asyncio
    async def test_subprocess_refuses_a_network_guarantee_it_cannot_give(self) -> None:
        from loom.runtime.sandbox import NetworkPolicy, SandboxBody, SandboxPolicy
        from loom.runtime.sandboxes import SubprocessSandbox

        sandbox = SubprocessSandbox(SandboxPolicy(network=NetworkPolicy.DENY))
        outcome = await sandbox.run(
            body=SandboxBody(invoke=_never, source="x = 1", entrypoint="x"),
            run_id="r",
            input=None,
            channel=_NullChannel(),
        )

        assert not outcome.ok
        assert outcome.violation == "policy"
        assert "network" in (outcome.error or "")

    def test_subprocess_does_not_claim_network(self) -> None:
        from loom.runtime.sandboxes import SubprocessSandbox

        assert "network" not in SubprocessSandbox().enforces

    def test_docker_does_claim_network(self) -> None:
        from loom.runtime.sandboxes.docker import DockerSandbox

        assert "network" in DockerSandbox(image="python:3.12-slim").enforces

    def test_unenforceable_is_derived_from_the_policy_not_a_hand_kept_list(
        self,
    ) -> None:
        """The defect's actual shape: `_unenforceable` inspected two field
        names, so every field added after it was silently dropped."""
        from loom.runtime.sandbox import NetworkPolicy, SandboxPolicy, unenforceable

        policy = SandboxPolicy(
            network=NetworkPolicy.DENY,
            allowed_imports=frozenset({"json"}),
            max_memory_mb=64,
        )

        assert unenforceable(policy, frozenset({"allowed_env"})) == [
            "allowed_imports",
            "max_memory_mb",
            "max_wall_seconds",
            "network",
        ]


async def _never() -> None:  # pragma: no cover - never invoked
    raise AssertionError("the inline path must not be reached")


class _NullChannel:
    async def dispatch(self, call: object) -> object:  # pragma: no cover
        raise AssertionError("no call should reach the channel")


class TestSandboxEnforcesImports:
    """`allowed_imports` was declared on the policy and read by nothing at all.
    Implemented rather than refused: the guard is ~20 lines in the child, and a
    second line of defence is worth more than a refusal."""

    @pytest.mark.asyncio
    async def test_a_permitted_import_succeeds(self) -> None:
        outcome = await _run_sandboxed(
            "import json\n"
            "async def flow(ctx, payload):\n"
            "    return json.dumps({'ok': True})\n",
            allowed={"json", "asyncio"},
        )
        assert outcome.ok, outcome.error

    @pytest.mark.asyncio
    async def test_an_unlisted_import_is_refused(self) -> None:
        outcome = await _run_sandboxed(
            "async def flow(ctx, payload):\n"
            "    import socket\n"
            "    return socket.gethostname()\n",
            allowed={"json"},
        )
        assert not outcome.ok
        assert "socket" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_no_policy_leaves_imports_alone(self) -> None:
        outcome = await _run_sandboxed(
            "async def flow(ctx, payload):\n"
            "    import socket\n"
            "    return 'imported'\n",
            allowed=None,
        )
        assert outcome.ok, outcome.error


async def _run_sandboxed(source: str, *, allowed: set[str] | None):
    from loom.runtime.sandbox import SandboxBody, SandboxPolicy
    from loom.runtime.sandboxes import SubprocessSandbox

    policy = SandboxPolicy(
        allowed_imports=None if allowed is None else frozenset(allowed),
        max_wall_seconds=30.0,
    )
    sandbox = SubprocessSandbox(policy)
    return await sandbox.run(
        body=SandboxBody(invoke=_never, source=source, entrypoint="flow"),
        run_id="r",
        input=None,
        channel=_NullChannel(),
    )


# ---------------------------------------------------------------------------
# H6 — mypy findings are about the generated file, or they are not findings
# ---------------------------------------------------------------------------


class TestTypeStageAttributesFindingsCorrectly:
    """The repo already solved this for itself in `scripts/typecheck.py` —
    "errors prevented further checking" means nothing was checked — and never
    applied the same defence to the stage that checks *generated* code. An
    environment with numpy installed reported a PEP 695 stub error at line 737
    of a fourteen-line workflow, and fed it to the repair loop."""

    def test_an_aborted_run_is_skipped_not_passed(self) -> None:
        from loom.agents.stages import _mypy_result

        result = _mypy_result(
            "types",
            Path("/tmp/x/generated.py"),
            "/site/numpy/__init__.pyi:737: error: Type statement is only "
            "supported in Python 3.12 and greater  [syntax]\n"
            "Found 1 error in 1 file (errors prevented further checking)\n",
        )

        assert result.skipped
        assert not result.issues

    def test_a_foreign_file_is_not_a_finding_about_this_code(self) -> None:
        from loom.agents.stages import _mypy_result

        result = _mypy_result(
            "types",
            Path("/tmp/x/generated.py"),
            "/site/numpy/__init__.pyi:737: error: bad stub  [syntax]\n",
        )

        assert result.issues == []
        assert not result.skipped

    def test_a_finding_about_this_file_is_reported(self) -> None:
        from loom.agents.stages import _mypy_result

        result = _mypy_result(
            "types",
            Path("/tmp/x/generated.py"),
            '/tmp/x/generated.py:12: error: Argument 1 has incompatible type "str"\n',
        )

        assert len(result.issues) == 1
        assert "incompatible type" in result.issues[0].message


# ---------------------------------------------------------------------------
# C2 — the smoke child holds no credentials
# ---------------------------------------------------------------------------


_PEEK_AT_SECRETS = '''
from loom import Context, step, workflow
import os

@step
async def peek() -> str:
    return "|".join(sorted(k for k in os.environ if k.endswith("_API_KEY")))

@workflow(name="p0_peeker")
async def peeker(ctx: Context, _inp=None) -> str:
    return await ctx.step(peek)
'''


class TestSmokeRunHoldsNoCredentials:
    """`smoke_run` inherited `os.environ` in full while the MCP tool that
    exposes it told the model "no real network or credentials". It executes
    code a *model* wrote."""

    def test_generated_code_cannot_read_the_host_api_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.agents.smoke import smoke_run

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak-either")

        result = smoke_run(_PEEK_AT_SECRETS, None)

        assert result.ok, result.error
        assert "API_KEY" not in result.output_preview

    def test_the_escape_hatch_still_inherits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.agents.smoke import SmokeIsolation, smoke_run

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-deliberately-visible")

        result = smoke_run(
            _PEEK_AT_SECRETS, None, isolation=SmokeIsolation(inherit_env=True)
        )

        assert "ANTHROPIC_API_KEY" in result.output_preview

    def test_the_allowlist_names_no_credential(self) -> None:
        from loom.agents.smoke import DEFAULT_SMOKE_ENV

        suspicious = [
            name
            for name in DEFAULT_SMOKE_ENV
            if "KEY" in name or "TOKEN" in name or "SECRET" in name
            or name.startswith("LOOM_") or "CREDENTIAL" in name
        ]
        assert suspicious == []

    def test_what_it_claims_matches_what_it_does(self) -> None:
        from loom.agents.smoke import DEFAULT_ISOLATION

        assert "no host credentials" in DEFAULT_ISOLATION.describe()
        assert "network is not restricted" in DEFAULT_ISOLATION.describe()


class TestAuthoringToolsThatActAreGated:
    """`_register_authoring_tools` is "not facade-scoped" — which held for the
    three that read a catalogue and did not for the three that execute code,
    write files, and call third-party APIs with this server's credentials."""

    def test_an_unconfigured_gate_refuses_nothing(self) -> None:
        from loom.mcp_server.authoring import ACTING_TOOLS, AuthoringGate

        gate = AuthoringGate()

        assert not gate.enforcing
        assert all(gate.refusal(tool) is None for tool in ACTING_TOOLS)

    def test_a_token_without_the_scope_is_refused(self) -> None:
        from loom.identity.principal import Principal
        from loom.mcp_server.authoring import ACTING_TOOLS, AuthoringGate

        gate = AuthoringGate(
            lambda: Principal(subject="u", scopes=frozenset({"runs:read"}))
        )

        for tool in ACTING_TOOLS:
            refusal = gate.refusal(tool)
            assert refusal is not None and "workflows:author" in refusal

    def test_the_reading_tools_stay_open(self) -> None:
        from loom.identity.principal import Principal
        from loom.mcp_server.authoring import AuthoringGate

        gate = AuthoringGate(
            lambda: Principal(subject="u", scopes=frozenset({"runs:read"}))
        )

        assert gate.refusal("get_tool_docs") is None
        assert gate.refusal("get_tool_contract") is None
        assert gate.refusal("validate_workflow_code") is None

    def test_the_right_scope_passes(self) -> None:
        from loom.identity.principal import Principal
        from loom.mcp_server.authoring import ACTING_TOOLS, AuthoringGate

        gate = AuthoringGate(
            lambda: Principal(subject="u", scopes=frozenset({"workflows:author"}))
        )

        assert all(gate.refusal(tool) is None for tool in ACTING_TOOLS)

    def test_no_token_at_all_is_refused_not_crashed(self) -> None:
        from loom.mcp_server.authoring import AuthoringGate

        def no_token() -> object:
            raise RuntimeError("no access token in this request")

        gate = AuthoringGate(no_token)
        refusal = gate.refusal("save_workflow")

        assert refusal is not None and "no access token" in refusal

    def test_every_acting_tool_is_a_real_authoring_capability(self) -> None:
        """Guards against the list drifting away from the module it describes."""
        from loom.mcp_server import authoring

        for tool in authoring.ACTING_TOOLS:
            assert callable(getattr(authoring, tool))
