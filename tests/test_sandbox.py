"""Where a workflow body runs, and what it can reach while it runs there.

Two adapters, one behavioural suite. That arrangement is the point rather than
tidiness: an isolating sandbox earns its keep only if a body running inside it
does *the same thing* it does inline, and the only way to keep that true is to
assert it of both from the same tests. A suite written per adapter drifts into
two definitions of correct.

The isolation tests are separate and deliberately asymmetric — ``InlineSandbox``
is *expected* to enforce nothing, and says so through ``enforces``. A test that
demanded isolation of both would either fail forever or push a fake enforcement
into the default path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from loom import Runtime, step, workflow
from loom.runtime.clock import ManualClock
from loom.runtime.context import Context
from loom.runtime.effects import DirectBroker, EffectCall, EffectResult
from loom.runtime.sandbox import (
    ExecutionSandbox,
    InlineSandbox,
    RuntimeChannel,
    SandboxBody,
    SandboxPolicy,
)
from loom.runtime.sandboxes.docker import DockerSandbox
from loom.runtime.sandboxes.subprocess import SubprocessSandbox
from loom.security.authority import Authority
from loom.stores.memory import MemoryStore

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="rlimits are POSIX; this sandbox declares so in enforces"
)


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
    except Exception:
        return False
    return result.returncode == 0


#: Computed once at collection time, not per test — `docker info` is a real
#: subprocess round trip and every skip decision below wants the same answer.
_DOCKER_AVAILABLE = _docker_daemon_available()


@pytest.fixture(scope="session")
def docker_test_image() -> Iterator[str]:
    """Installs *this checkout's* `loom` plus `pytest` over `python:3.12-slim`
    — not the real `deployment/docker-sandbox/Dockerfile`.

    Two things a production sandbox image never needs, both artifacts of how
    this conformance suite recovers a body's source: `Runtime._sandbox_source`
    reads the whole *module* a workflow is defined in (see its own docstring),
    so exec'ing this test module inside the container re-runs every import at
    its top — `pytest` (this file imports it) and `loom` itself (`from loom
    import Runtime, step, workflow`). A generated production workflow module
    imports neither, which is exactly why the real Dockerfile installs only
    `loomflow`/`pydantic` and nothing test-related.

    Built once per session against this repository as the build context (so
    local, possibly-unreleased changes to `loom` are what the container runs
    against — the same property `DockerSandbox`'s own docstring asks of a real
    deployment) and torn down after. A build failure (no network access to
    pull the base image or resolve `loom`'s own dependencies) skips rather
    than fails, since this is an environment gap the suite does not exist to
    catch.
    """
    tag = f"loom-sandbox-test-{uuid.uuid4().hex[:8]}"
    dockerfile = (
        "FROM python:3.12-slim\n"
        "COPY pyproject.toml README.md /tmp/loom/\n"
        "COPY src/ /tmp/loom/src/\n"
        "RUN pip install --no-cache-dir --no-compile pytest /tmp/loom "
        "&& rm -rf /tmp/loom\n"
        "RUN useradd --create-home --shell /usr/sbin/nologin sandbox\n"
    )
    repo_root = Path(__file__).resolve().parent.parent
    build = subprocess.run(
        ["docker", "build", "-t", tag, "-f", "-", str(repo_root)],
        input=dockerfile.encode(),
        capture_output=True,
    )
    if build.returncode != 0:
        pytest.skip(
            "could not build a Docker test image: "
            f"{build.stderr.decode(errors='replace')}"
        )
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


# -- the workflow both adapters run ------------------------------------------
#
# Defined at module scope, not in a fixture: SubprocessSandbox runs the body
# from this module's *source*, so a body defined inside a function would be
# invisible to it — the sandbox would refuse rather than run something else,
# which is correct behaviour and a useless test.


@step
async def double(value: int) -> int:
    return value * 2


@step
async def add_ten(value: int) -> int:
    return value + 10


@workflow(name="arithmetic")
async def arithmetic(ctx: Context, payload: dict) -> int:
    doubled = await ctx.step(double, value=payload["value"])
    return await ctx.step(add_ten, value=doubled)


@pytest.fixture(params=["inline", "subprocess", "docker"])
def sandbox(request) -> ExecutionSandbox:
    """Every behavioural test runs against all three. Adding a fourth adapter
    means adding it here, and finding out immediately what it does
    differently.

    The Docker branch requests its image lazily
    (`request.getfixturevalue`) rather than depending on
    ``docker_test_image`` directly, so the ``inline``/``subprocess``
    parametrizations never pay for a container build they do not use.
    """
    if request.param == "inline":
        return InlineSandbox()
    if request.param == "subprocess":
        return SubprocessSandbox()
    if not _DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    image = request.getfixturevalue("docker_test_image")
    return DockerSandbox(image)


class TestBothAdaptersAgree:
    """The conformance suite. Anything here must hold for every sandbox."""

    async def test_a_body_produces_the_same_result(self, sandbox) -> None:
        rt = Runtime(store=MemoryStore(), sandbox=sandbox)
        rt.register(arithmetic)

        result = await rt.run(arithmetic, {"value": 5})

        assert result.output == 20, f"{sandbox.name} computed something else"

    async def test_the_journal_is_identical(self) -> None:
        """The property that makes a sandbox swappable rather than a fork.

        Compared as (kind, name, output) triples rather than whole entries:
        timestamps and run ids differ between any two runs, and asserting on
        them would fail for reasons that have nothing to do with the sandbox.
        """

        async def shape_of(sandbox: ExecutionSandbox) -> list[tuple[str, str, object]]:
            rt = Runtime(store=MemoryStore(), sandbox=sandbox)
            rt.register(arithmetic)
            result = await rt.run(arithmetic, {"value": 5})
            entries = await rt.store.load_journal(result.run_id)
            return [(e.kind.value, e.name, e.output) for e in entries]

        assert await shape_of(SubprocessSandbox()) == await shape_of(InlineSandbox())

    async def test_every_durable_call_reaches_the_broker(self, sandbox) -> None:
        """Sandboxed must never mean unmediated.

        A sandbox that proxied calls anywhere but the broker chain would leave
        grants, budgets, dry-run, and taint applying to inline runs only — and
        the deployment most likely to want a sandbox is the one that most needs
        those to hold.
        """
        seen: list[EffectCall] = []

        class Recording(DirectBroker):
            async def dispatch(
                self, call: EffectCall, authority: Authority
            ) -> EffectResult:
                seen.append(call)
                return await super().dispatch(call, authority)

        rt = Runtime(store=MemoryStore(), sandbox=sandbox, broker=Recording())
        rt.register(arithmetic)

        await rt.run(arithmetic, {"value": 5})

        assert [c.target for c in seen] == ["double", "add_ten"], (
            f"{sandbox.name} did not route both steps through the broker"
        )

    async def test_a_failing_step_fails_the_run(self, sandbox) -> None:
        """An error inside the body is a failed run, not a lost one."""
        rt = Runtime(store=MemoryStore(), sandbox=sandbox)
        rt.register(explodes)

        result = await rt.run(explodes, {})

        assert result.status.value == "failed"
        assert "detonated" in (result.error.message if result.error else "")

    async def test_the_sandbox_declares_what_it_enforces(self, sandbox) -> None:
        """``enforces`` is a claim a host can check before trusting it.

        A sandbox that accepted ``max_memory_mb`` and could not apply it would
        leave a host believing in a bound that is not there.
        """
        assert isinstance(sandbox.enforces, frozenset)
        assert sandbox.enforces <= set(vars(SandboxPolicy())) | {
            f.name for f in SandboxPolicy.__dataclass_fields__.values()
        }


@step
async def detonate() -> None:
    raise RuntimeError("detonated")


@workflow(name="explodes")
async def explodes(ctx: Context, payload: dict) -> None:
    await ctx.step(detonate)


class TestTheDefaultIsUnchanged:
    """A Runtime nobody configured must behave exactly as it did before."""

    def test_a_bare_runtime_runs_inline(self) -> None:
        assert isinstance(Runtime(store=MemoryStore()).sandbox, InlineSandbox)

    def test_the_default_enforces_nothing_and_says_so(self) -> None:
        assert Runtime(store=MemoryStore()).sandbox.enforces == frozenset()

    async def test_control_flow_still_propagates(self) -> None:
        """Parking is not an outcome.

        An adapter that caught exceptions and repackaged them as
        ``SandboxOutcome(ok=False)`` would turn every human approval into a
        failure — on the path every default Runtime takes.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(parks)

        result = await rt.run(parks, {})

        assert result.status.value == "suspended"


@workflow(name="parks")
async def parks(ctx: Context, payload: dict) -> str:
    await ctx.wait_for_event("go")
    return "resumed"


@workflow(name="reports_pid")
async def reports_pid(ctx: Context, payload: dict) -> int:
    return os.getpid()


class TestIsolation:
    """Only the subprocess adapter. Inline is expected to enforce nothing."""

    async def test_the_engine_really_leaves_the_process(self) -> None:
        """That the seam is *used*, not merely present.

        Every other test here compares a sandboxed run to an inline one, and an
        engine that ignored ``Runtime(sandbox=...)`` and ran everything inline
        would satisfy all of them — equivalence is exactly what bypassing the
        sandbox produces. Reverting the engine to call ``definition.invoke``
        directly passed the whole suite until this test existed. A pid is the
        cheapest fact the two modes cannot agree on.
        """
        rt = Runtime(store=MemoryStore(), sandbox=SubprocessSandbox())
        rt.register(reports_pid)

        result = await rt.run(reports_pid, {})

        assert result.output != os.getpid(), (
            "the body ran in this process: Runtime(sandbox=...) was not honoured"
        )

    async def test_inline_stays_in_this_process(self) -> None:
        """The other half of the same claim, so neither can drift silently."""
        rt = Runtime(store=MemoryStore(), sandbox=InlineSandbox())
        rt.register(reports_pid)

        result = await rt.run(reports_pid, {})

        assert result.output == os.getpid()

    @pytest.fixture
    def secret_in_the_environment(self, monkeypatch) -> str:
        monkeypatch.setenv("LOOM_TEST_SECRET", "hunter2")
        return "hunter2"

    async def test_the_child_cannot_read_an_unallowed_variable(
        self, secret_in_the_environment
    ) -> None:
        """The failure this exists to prevent: code the host did not write
        reading a credential the host holds."""
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "import os\n"
                    "async def peek(ctx, payload):\n"
                    "    return os.environ.get('LOOM_TEST_SECRET', 'absent')\n"
                ),
                entrypoint="peek",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(),
        )

        assert outcome.ok
        assert outcome.value == "absent", "the secret crossed into the sandbox"

    async def test_an_allowed_variable_is_passed_through(
        self, secret_in_the_environment
    ) -> None:
        """The allowlist has to actually allow, or hosts will stop using it."""
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "import os\n"
                    "async def peek(ctx, payload):\n"
                    "    return os.environ.get('LOOM_TEST_SECRET', 'absent')\n"
                ),
                entrypoint="peek",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(allowed_env=frozenset({"LOOM_TEST_SECRET"})),
        )

        assert outcome.value == "hunter2"

    async def test_a_body_that_never_returns_is_killed(self) -> None:
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def spin(ctx, payload):\n"
                    "    while True:\n"
                    "        pass\n"
                ),
                entrypoint="spin",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=1.0),
        )

        assert not outcome.ok
        assert outcome.violation == "max_wall_seconds"

    async def test_a_runaway_allocation_is_bounded_or_the_limit_is_refused(
        self,
    ) -> None:
        """Two acceptable answers, and one that is not.

        Where ``RLIMIT_AS`` works the allocation is stopped. Where it does not —
        macOS has the constant and rejects every finite value, including a
        *lower* one — the sandbox must refuse the policy rather than run the
        body without the cap. Accepting ``max_memory_mb`` and ignoring it is the
        failure this asserts against: the host would believe untrusted code was
        bounded when nothing bounded it.
        """
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def gorge(ctx, payload):\n"
                    "    blocks = []\n"
                    "    for _ in range(10000):\n"
                    "        blocks.append(bytearray(10 * 1024 * 1024))\n"
                    "    return len(blocks)\n"
                ),
                entrypoint="gorge",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_memory_mb=128, max_wall_seconds=30.0),
        )

        assert not outcome.ok, "the allocation was neither bounded nor refused"
        if "max_memory_mb" in SubprocessSandbox().enforces:
            assert "MemoryError" in outcome.error or outcome.violation
        else:
            assert outcome.violation == "policy"
            assert "max_memory_mb" in outcome.error

    async def test_an_unenforceable_limit_is_named_not_dropped(self) -> None:
        """The refusal has to say which limit and what is available, or a host
        reads it as "sandboxing is broken" instead of "not this limit, here"."""
        sandbox = SubprocessSandbox()
        missing = set(SandboxPolicy.__dataclass_fields__) - sandbox.enforces
        if not {"max_memory_mb", "max_cpu_seconds"} & missing:
            pytest.skip(f"{sys.platform} enforces every rlimit; nothing to refuse")

        outcome = await sandbox.run(
            body=SandboxBody(invoke=_unused, source="async def f(ctx, p): return 1"),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_memory_mb=64),
        )

        assert not outcome.ok
        assert "max_memory_mb" in outcome.error
        assert sys.platform in outcome.error

    async def test_a_body_with_no_source_is_refused_not_substituted(self) -> None:
        """The one outcome a sandbox must never have: running something else
        because the real body could not be recovered."""
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(invoke=_unused, source="", entrypoint="gone"),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(),
        )

        assert not outcome.ok
        assert outcome.violation == "source"
        assert "inline" in outcome.error

    @POSIX_ONLY
    def test_cpu_time_is_declared_on_every_posix_platform(self) -> None:
        """``RLIMIT_CPU`` is the one rlimit macOS and Linux both honour."""
        assert "max_cpu_seconds" in SubprocessSandbox().enforces


class TestTheChannelIsTheOnlyWayOut:
    """``RuntimeChannel`` is where a proxied call becomes real work."""

    async def test_an_unknown_step_is_refused_not_raised(self) -> None:
        """A body naming a step that does not exist fails like a denied effect.

        Raising here would crash the conversation with the child, and the run
        would fail with a pipe error rather than with the reason.
        """
        channel = RuntimeChannel(ctx=None, steps={})

        result = await channel.dispatch(EffectCall(kind="step", target="nope"))

        assert not result.ok
        assert "no step named 'nope'" in (result.error or "")

    async def test_an_unsupported_kind_is_refused(self) -> None:
        channel = RuntimeChannel(ctx=None, steps={})

        result = await channel.dispatch(EffectCall(kind="artifact", target="report"))

        assert not result.ok
        assert "cannot perform 'artifact'" in (result.error or "")

    async def test_only_the_workflows_own_module_steps_are_reachable(self) -> None:
        """The map is the allowlist.

        A process-wide step registry would hand every sandboxed workflow every
        step any import had ever defined — including one from a package the
        body was never meant to touch.
        """
        rt = Runtime(store=MemoryStore())
        rt.register(arithmetic)

        reachable = rt._sandbox_steps(arithmetic)

        assert {"double", "add_ten", "detonate"} <= set(reachable)
        assert "gmail_send_message" not in reachable


@step
async def bridge_call(operation_id: str, **kwargs: Any) -> str:
    """Stands in for `pipeshub_tool`: one generic step fronting many logical
    operations, distinguished only by the `name=` override at the call site."""
    return f"did {operation_id}"


@workflow(name="bridged")
async def bridged(ctx: Context, payload: dict) -> str:
    return await ctx.step(bridge_call, name=payload["op"], operation_id=payload["op"])


class TestNameOverrideOnTheWire:
    """PipesHub journals a bridged call under its logical name, not the
    generic step's own name. The override has to survive the wire or a
    Docker/subprocess run's journal permanently diverges from an inline
    one's."""

    async def test_the_journal_uses_the_override_name(self) -> None:
        rt = Runtime(store=MemoryStore(), sandbox=InlineSandbox())
        rt.register(bridged)

        result = await rt.run(bridged, {"op": "jira.create_issue"})

        entries = await rt.store.load_journal(result.run_id)
        assert [e.name for e in entries] == ["jira.create_issue"]

    async def test_inline_and_subprocess_journal_identically_with_override(self) -> None:
        async def shape_of(sandbox: ExecutionSandbox) -> list[tuple[str, str, object]]:
            rt = Runtime(store=MemoryStore(), sandbox=sandbox)
            rt.register(bridged)
            result = await rt.run(bridged, {"op": "jira.create_issue"})
            entries = await rt.store.load_journal(result.run_id)
            return [(e.kind.value, e.name, e.output) for e in entries]

        assert await shape_of(SubprocessSandbox()) == await shape_of(InlineSandbox())


@step
async def injected_step(x: int) -> int:
    return x + 100


class TestHostInjectedSteps:
    """`Runtime(sandbox_steps=...)` is how a host makes a step reachable that
    is not a name in the workflow's own module — a dynamically-bridged tool
    step, in PipesHub's case."""

    async def test_sandbox_steps_are_reachable_and_callable(self) -> None:
        exec_source = (
            "from loom import workflow\n"
            "\n"
            "@workflow(name='exec_flow')\n"
            "async def flow(ctx, payload):\n"
            "    return await ctx.step('injected_step', x=payload['x'])\n"
        )
        namespace: dict[str, Any] = {}
        exec(compile(exec_source, "<test>", "exec"), namespace)
        definition = namespace["flow"]

        rt = Runtime(
            store=MemoryStore(),
            sandbox=SubprocessSandbox(),
            sandbox_steps={"injected_step": injected_step},
        )
        await rt.publish(definition, source=exec_source)

        result = await rt.run(definition, {"x": 5})

        assert result.output == 105, result.error

    async def test_a_step_outside_the_map_is_still_refused(self) -> None:
        exec_source = (
            "from loom import workflow\n"
            "\n"
            "@workflow(name='exec_flow_unreachable')\n"
            "async def flow(ctx, payload):\n"
            "    return await ctx.step('not_injected', x=1)\n"
        )
        namespace: dict[str, Any] = {}
        exec(compile(exec_source, "<test>", "exec"), namespace)
        definition = namespace["flow"]

        rt = Runtime(
            store=MemoryStore(),
            sandbox=SubprocessSandbox(),
            sandbox_steps={"injected_step": injected_step},
        )
        await rt.publish(definition, source=exec_source)

        result = await rt.run(definition, {})

        assert result.status.value == "failed"
        assert "not_injected" in (result.error.message if result.error else "")

    async def test_a_bare_callable_can_be_injected_too(self) -> None:
        """PipesHub's bridge step (``pipeshub_tool``) is a plain async
        function, not a ``@step``-decorated ``StepDefinition`` — the same
        shape ``Context.step`` already accepts directly. Filtering the
        injected map by ``isinstance(..., StepDefinition)`` would silently
        make exactly that case unreachable."""

        async def bare_bridge(operation_id: str, **kwargs: Any) -> str:
            return f"bridged {operation_id}"

        exec_source = (
            "from loom import workflow\n"
            "\n"
            "@workflow(name='exec_flow_bare')\n"
            "async def flow(ctx, payload):\n"
            "    return await ctx.step("
            "'bare_bridge', name=payload['op'], operation_id=payload['op'])\n"
        )
        namespace: dict[str, Any] = {}
        exec(compile(exec_source, "<test>", "exec"), namespace)
        definition = namespace["flow"]

        rt = Runtime(
            store=MemoryStore(),
            sandbox=SubprocessSandbox(),
            sandbox_steps={"bare_bridge": bare_bridge},
        )
        await rt.publish(definition, source=exec_source)

        result = await rt.run(definition, {"op": "jira.create_issue"})

        assert result.output == "bridged jira.create_issue", result.error
        entries = await rt.store.load_journal(result.run_id)
        assert [e.name for e in entries] == ["jira.create_issue"]

    def test_local_step_helpers_in_exec_source_are_reachable(self) -> None:
        """The `fn.__globals__` fix. A workflow compiled via `exec()` has no
        module — `inspect.getmodule(fn)` is always `None` — so a helper
        `@step` defined alongside it in the same exec'd namespace must be
        found by scanning `fn.__globals__` instead."""
        exec_source = (
            "from loom import workflow, step\n"
            "\n"
            "@step\n"
            "async def local_helper(x: int) -> int:\n"
            "    return x\n"
            "\n"
            "@workflow(name='exec_flow_local_step')\n"
            "async def flow(ctx, payload):\n"
            "    return await ctx.step(local_helper, x=payload['x'])\n"
        )
        namespace: dict[str, Any] = {}
        exec(compile(exec_source, "<test>", "exec"), namespace)
        definition = namespace["flow"]

        reachable = Runtime(store=MemoryStore())._sandbox_steps(definition)

        assert "local_helper" in reachable


class TestChildOwnedLocalSteps:
    """A host that never ``exec()``s generated source (PipesHub's Docker
    path) registers a stub whose globals contain no ``@step`` helpers. Those
    helpers still exist in the published source the child exec's, and must
    run *there* — running them on the parent would undo the sandbox — while
    still journaling so a non-deterministic helper is memoized on replay."""

    _SOURCE = (
        "from loom import workflow, step\n"
        "\n"
        "@step\n"
        "async def generate_random_integer(low: int, high: int) -> int:\n"
        "    return low + (high - low) // 2\n"
        "\n"
        "@workflow(name='rng_flow')\n"
        "async def flow(ctx, payload):\n"
        "    return await ctx.step("
        "generate_random_integer, low=payload['low'], high=payload['high'])\n"
    )

    async def test_a_local_step_absent_from_the_parent_runs_in_the_child(
        self, sandbox: ExecutionSandbox,
    ) -> None:
        if sandbox.name == "inline":
            pytest.skip("inline invokes the stub; there is no child to run the helper")

        from loom.runtime.workflow import WorkflowDefinition

        async def stub(ctx: Context, payload: dict) -> int:
            raise AssertionError("the parent stub must not run")

        definition = WorkflowDefinition(fn=stub, name="rng_flow")
        rt = Runtime(store=MemoryStore(), sandbox=sandbox)
        rt.register(definition)
        await rt.publish(definition, source=self._SOURCE)

        result = await rt.run(definition, {"low": 1, "high": 9})

        assert result.output == 5, result.error
        entries = await rt.store.load_journal(result.run_id)
        assert [e.name for e in entries] == ["generate_random_integer"]

    async def test_a_child_local_step_is_served_from_the_journal_on_replay(
        self, sandbox: ExecutionSandbox,
    ) -> None:
        if sandbox.name == "inline":
            pytest.skip("inline invokes the stub; there is no child to run the helper")

        from loom.runtime.workflow import WorkflowDefinition

        async def stub(ctx: Context, payload: dict) -> int:
            raise AssertionError("the parent stub must not run")

        definition = WorkflowDefinition(fn=stub, name="rng_flow_replay")
        source = self._SOURCE.replace("rng_flow", "rng_flow_replay")
        rt = Runtime(store=MemoryStore(), sandbox=sandbox)
        rt.register(definition)
        await rt.publish(definition, source=source)

        first = await rt.run(definition, {"low": 1, "high": 9})
        replayed = await rt.replay(first.run_id)

        assert first.output == replayed.output == 5


@workflow(name="sleeps_durably")
async def sleeps_durably(ctx: Context, payload: dict) -> str:
    await ctx.sleep(300)
    return "done"


@workflow(name="reports_progress")
async def reports_progress(ctx: Context, payload: dict) -> str:
    await ctx.report("halfway there")
    return "done"


class TestTheChildCtxSurfaceIsComplete:
    """`ctx.sleep` and `ctx.report` are ordinary parts of a workflow body —
    without them in the child stub, any generated workflow that sleeps or
    reports progress fails the moment it runs in a non-inline sandbox."""

    async def test_sleep_parks_the_run_and_resume_completes_it(self) -> None:
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        rt = Runtime(store=MemoryStore(), sandbox=SubprocessSandbox(), clock=clock)
        rt.register(sleeps_durably)

        parked = await rt.run(sleeps_durably, {})
        assert parked.status.value == "suspended"

        clock.advance(seconds=301)
        resumed = await rt.resume(parked.run_id)

        assert resumed.status.value == "completed"
        assert resumed.output == "done"

    async def test_report_reaches_the_parents_stream(self) -> None:
        rt = Runtime(store=MemoryStore(), sandbox=SubprocessSandbox())
        rt.register(reports_progress)

        result = await rt.run(reports_progress, {})

        seen = rt.stream.since(result.run_id)
        assert any(r.message == "halfway there" for r in seen)


class TestProtocolRobustness:
    """Two latent bugs that only bite model-generated code: a stray `print()`
    and a payload bigger than asyncio's default stream buffer."""

    async def test_print_does_not_corrupt_the_wire(self) -> None:
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def noisy(ctx, payload):\n"
                    "    print('this would corrupt the wire if not redirected')\n"
                    "    print({'not': 'necessarily json-safe either'})\n"
                    "    return 'ok'\n"
                ),
                entrypoint="noisy",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "ok"

    async def test_agent_result_text_survives_the_wire(self) -> None:
        """Generated bodies call ``result.text()``; the wire carries a dict.

        Importing ``AgentResult`` into the child would put parent types on
        the untrusted side. The harness wraps the encoded dict so the call
        the coding-agent prompt teaches still works.
        """
        from loom.agents.result import AgentResult

        class _AgentChannel:
            async def dispatch(self, call: EffectCall) -> EffectResult:
                assert call.kind == "agent"
                return EffectResult(
                    value=AgentResult(output="knock knock", agent="jester")
                )

        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def flow(ctx, payload):\n"
                    "    joke = await ctx.agent('tell a joke')\n"
                    "    return {'text': joke.text(), 'output': joke.output}\n"
                ),
                entrypoint="flow",
            ),
            run_id="r1",
            input={},
            channel=_AgentChannel(),
            policy=SandboxPolicy(),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == {"text": "knock knock", "output": "knock knock"}

    async def test_namespace_is_visible_to_the_source(self) -> None:
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source="async def read_it(ctx, payload):\n    return greeting\n",
                entrypoint="read_it",
                namespace={"greeting": "hello from the host"},
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "hello from the host"

    async def test_entrypoint_falls_back_to_the_sole_workflow(self) -> None:
        """A renamed definition (PipesHub compiles every version under a
        synthetic name) is still bound in the exec'd namespace under its
        original function name — the fallback is what finds it anyway."""
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "from loom import workflow\n"
                    "\n"
                    "@workflow(name='original')\n"
                    "async def original_fn(ctx, payload):\n"
                    "    return 'found via fallback'\n"
                ),
                entrypoint="pipeshub-v-does-not-match-fn-name",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "found via fallback"

    async def test_entrypoint_refuses_rather_than_guesses_when_ambiguous(self) -> None:
        outcome = await SubprocessSandbox().run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "from loom import workflow\n"
                    "\n"
                    "@workflow(name='one')\n"
                    "async def one_fn(ctx, payload):\n"
                    "    return 1\n"
                    "\n"
                    "@workflow(name='two')\n"
                    "async def two_fn(ctx, payload):\n"
                    "    return 2\n"
                ),
                entrypoint="missing",
            ),
            run_id="r1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(),
        )

        assert not outcome.ok
        assert "2 workflow-like candidates" in outcome.error


@step
async def big_result() -> str:
    return "x" * (100 * 1024)


@workflow(name="big_payload")
async def big_payload(ctx: Context, payload: dict) -> str:
    return await ctx.step(big_result)


class TestLargePayloads:
    async def test_a_step_result_over_64kib_round_trips(self) -> None:
        """`create_subprocess_exec` defaults its stream buffer to 64 KiB;
        without an explicit `limit=`, the child's final `_emit` of a larger
        value raises `LimitOverrunError` on the parent's `readline()`."""
        rt = Runtime(store=MemoryStore(), sandbox=SubprocessSandbox())
        rt.register(big_payload)

        result = await rt.run(big_payload, {})

        assert result.output == "x" * (100 * 1024)


async def _unused() -> None:  # pragma: no cover - the callable half is unused here
    raise AssertionError("the subprocess adapter must not invoke the local callable")


class _NoChannel:
    """For bodies that perform no durable call. Any dispatch is a test bug."""

    async def dispatch(self, call: EffectCall) -> EffectResult:  # pragma: no cover
        raise AssertionError(f"unexpected durable call: {call.target}")


def test_the_module_source_is_readable() -> None:
    """Guards the suite itself.

    Every subprocess test above depends on this module's source being
    recoverable; if it stopped being, they would pass by refusing rather than
    by isolating, and the suite would go quietly green while testing nothing.
    """
    assert "async def arithmetic" in __import__("inspect").getsource(sys.modules[__name__])
