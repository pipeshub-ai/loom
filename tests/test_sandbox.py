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
import sys

import pytest

from loom import Runtime, step, workflow
from loom.runtime.context import Context
from loom.runtime.effects import DirectBroker, EffectCall, EffectResult
from loom.runtime.sandbox import (
    ExecutionSandbox,
    InlineSandbox,
    RuntimeChannel,
    SandboxBody,
    SandboxPolicy,
)
from loom.runtime.sandboxes.subprocess import SubprocessSandbox
from loom.security.authority import Authority
from loom.stores.memory import MemoryStore

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="rlimits are POSIX; this sandbox declares so in enforces"
)


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


@pytest.fixture(params=["inline", "subprocess"])
def sandbox(request) -> ExecutionSandbox:
    """Every behavioural test runs against both. Adding a third adapter means
    adding it here, and finding out immediately what it does differently."""
    return InlineSandbox() if request.param == "inline" else SubprocessSandbox()


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
