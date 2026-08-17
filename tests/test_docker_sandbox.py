"""What Docker enforces that a bare subprocess does not.

`tests/test_sandbox.py` already runs the full behavioural conformance suite
against ``DockerSandbox`` (parametrized alongside ``inline``/``subprocess``) —
same result, same journal, same broker routing. This file is the asymmetric
half: the isolation properties only a container actually provides — network,
memory, filesystem, and capabilities — none of which ``SubprocessSandbox``
claims (see its own ``enforces``).

Every test here needs a real Docker daemon and is skipped-and-named, never
silently dropped, when one is unreachable — the same rule the store
conformance suite applies. A suite that quietly shrinks reports green for
coverage it does not have.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from loom.core.exceptions import Suspend
from loom.runtime.effects import EffectCall, EffectResult
from loom.runtime.sandbox import SandboxBody, SandboxPolicy
from loom.runtime.sandboxes.docker import DockerSandbox


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
    except Exception:
        return False
    return result.returncode == 0


_DOCKER_AVAILABLE = _docker_daemon_available()

pytestmark = pytest.mark.skipif(
    not _DOCKER_AVAILABLE, reason="docker daemon is not reachable in this environment"
)


@pytest.fixture(scope="session")
def sandbox_image() -> Iterator[str]:
    """A trivial image good enough for every test below: a `sandbox` user and
    a stock `python3` over `python:3.12-slim`. Not the real
    `deployment/docker-sandbox/Dockerfile`, which additionally installs
    `loomflow`/`pydantic` that no body here imports."""
    tag = f"loom-docker-sandbox-test-{uuid.uuid4().hex[:8]}"
    dockerfile = (
        "FROM python:3.12-slim\n"
        "RUN useradd --create-home --shell /usr/sbin/nologin sandbox\n"
    )
    build = subprocess.run(
        ["docker", "build", "-t", tag, "-"],
        input=dockerfile.encode(),
        capture_output=True,
    )
    if build.returncode != 0:
        pytest.skip(
            f"could not build a Docker test image: {build.stderr.decode(errors='replace')}"
        )
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


async def _unused() -> None:  # pragma: no cover - the callable half is unused here
    raise AssertionError("DockerSandbox must not invoke the local callable")


class _NoChannel:
    """For bodies that perform no durable call. Any dispatch is a test bug."""

    async def dispatch(self, call: EffectCall) -> EffectResult:  # pragma: no cover
        raise AssertionError(f"unexpected durable call: {call.target}")


class _RecordingChannel:
    """Records every proxied call and answers with a canned or default reply."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.seen: list[EffectCall] = []
        self._replies = replies or {}

    async def dispatch(self, call: EffectCall) -> EffectResult:
        self.seen.append(call)
        key = call.name or call.target
        if key in self._replies:
            reply = self._replies[key]
            if isinstance(reply, Exception):
                raise reply
            return EffectResult(value=reply)
        return EffectResult(value=None)


class _ParkingChannel:
    """Raises `Suspend` on the first dispatch — stands in for a real
    `RuntimeChannel` parking the run on `ctx.wait_for_event`/`ctx.sleep`."""

    async def dispatch(self, call: EffectCall) -> EffectResult:
        raise Suspend("parked for a test", path=call.target)


class TestNetworkIsolation:
    async def test_socket_connect_fails_inside_the_container(self, sandbox_image: str) -> None:
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "import socket\n"
                    "async def reach_out(ctx, payload):\n"
                    "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                    "    s.settimeout(3)\n"
                    "    try:\n"
                    "        s.connect(('8.8.8.8', 53))\n"
                    "        return 'connected'\n"
                    "    except OSError as exc:\n"
                    "        return 'blocked: %s' % exc\n"
                ),
                entrypoint="reach_out",
            ),
            run_id="net-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value.startswith("blocked"), (
            "a body reached the network from inside --network none"
        )

    async def test_network_true_is_refused_before_anything_is_spawned(
        self, sandbox_image: str
    ) -> None:
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(invoke=_unused, source="async def f(ctx, p): return 1"),
            run_id="net-2",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(network=True),
        )

        assert not outcome.ok
        assert outcome.violation == "policy"
        assert "network" in outcome.error


class TestMemoryCgroup:
    async def test_a_runaway_allocation_is_stopped_at_the_declared_limit(
        self, sandbox_image: str
    ) -> None:
        """Unlike `SubprocessSandbox` on macOS (`RLIMIT_AS` refused), a
        container's cgroup memory accounting works identically on every host
        this daemon runs on."""
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
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
            run_id="mem-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_memory_mb=128, max_wall_seconds=30),
        )

        assert not outcome.ok, "the allocation was not stopped"
        assert "max_memory_mb" in sandbox.enforces


class TestWallTimeout:
    async def test_a_body_that_never_returns_is_killed_and_its_container_removed(
        self, sandbox_image: str
    ) -> None:
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def spin(ctx, payload):\n"
                    "    while True:\n"
                    "        pass\n"
                ),
                entrypoint="spin",
            ),
            run_id="wall-timeout",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=1.0),
        )

        assert not outcome.ok
        assert outcome.violation == "max_wall_seconds"

        check = await asyncio.create_subprocess_exec(
            "docker", "ps", "-aq", "--filter", "name=loom-sbx-wall-timeout-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await check.communicate()
        assert stdout.decode().strip() == "", "the container outlived the timeout"


class TestCpuSeconds:
    async def test_cpu_exhaustion_stops_the_container(self, sandbox_image: str) -> None:
        """`ulimit -t` inside the container, not `--cpus` (which caps CPU
        *rate*, not total seconds consumed) — see `DockerSandbox`'s own
        docstring."""
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def burn(ctx, payload):\n"
                    "    total = 0\n"
                    "    while True:\n"
                    "        total += 1\n"
                ),
                entrypoint="burn",
            ),
            run_id="cpu-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_cpu_seconds=1, max_wall_seconds=30),
        )

        assert not outcome.ok, "CPU exhaustion did not stop the body"


class TestOrphanSweep:
    async def test_sweep_removes_a_stale_labeled_container(self, sandbox_image: str) -> None:
        label = f"loom-test-{uuid.uuid4().hex[:8]}"
        sandbox = DockerSandbox(sandbox_image, container_label=label)
        name = f"loom-sbx-orphan-{uuid.uuid4().hex[:8]}"

        instance_id = os.environ.get("HOSTNAME") or f"pid-{os.getpid()}"
        create = await asyncio.create_subprocess_exec(
            "docker", "run", "-d",
            "--label", f"{label}=1",
            "--label", f"{label}-instance={instance_id}",
            "--name", name, sandbox_image, "sleep", "300",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await create.communicate()
        if create.returncode != 0:
            pytest.skip(f"could not start a throwaway container: {stderr.decode(errors='replace')}")

        try:
            removed = await sandbox.sweep_orphans()
            assert removed >= 1

            check = await asyncio.create_subprocess_exec(
                "docker", "ps", "-aq", "--filter", f"name={name}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await check.communicate()
            assert stdout.decode().strip() == ""
        finally:
            cleanup = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()

    async def test_sweep_is_a_no_op_when_nothing_is_labeled(self, sandbox_image: str) -> None:
        label = f"loom-test-{uuid.uuid4().hex[:8]}"
        sandbox = DockerSandbox(sandbox_image, container_label=label)

        removed = await sandbox.sweep_orphans()

        assert removed == 0

    async def test_a_missing_docker_binary_returns_zero_instead_of_raising(self) -> None:
        sandbox = DockerSandbox("unused:latest", docker_binary="definitely-not-a-real-binary-xyz")

        removed = await sandbox.sweep_orphans()

        assert removed == 0


class TestSuspendPropagates:
    async def test_wait_for_event_suspend_propagates_out_of_run(self, sandbox_image: str) -> None:
        """A dispatch that parks the run must raise straight through
        `run()`, not come back as a failed `SandboxOutcome` — flattening the
        two turns every human approval into an outage."""
        sandbox = DockerSandbox(sandbox_image)
        with pytest.raises(Suspend):
            await sandbox.run(
                body=SandboxBody(
                    invoke=_unused,
                    source=(
                        "async def waits(ctx, payload):\n"
                        "    return await ctx.wait_for_event('go')\n"
                    ),
                    entrypoint="waits",
                ),
                run_id="park-1",
                input={},
                channel=_ParkingChannel(),
                policy=SandboxPolicy(max_wall_seconds=15),
            )

    async def test_the_container_is_gone_after_a_park(self, sandbox_image: str) -> None:
        sandbox = DockerSandbox(sandbox_image)
        with pytest.raises(Suspend):
            await sandbox.run(
                body=SandboxBody(
                    invoke=_unused,
                    source="async def sleeps(ctx, payload):\n    return await ctx.sleep(60)\n",
                    entrypoint="sleeps",
                ),
                run_id="park-container-check",
                input={},
                channel=_ParkingChannel(),
                policy=SandboxPolicy(max_wall_seconds=15),
            )

        check = await asyncio.create_subprocess_exec(
            "docker", "ps", "-aq", "--filter", "name=loom-sbx-park-container-check-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await check.communicate()
        assert stdout.decode().strip() == "", "the container outlived the parked run"


class TestUnknownStepRefused:
    async def test_an_unresolvable_target_is_refused_not_raised(self, sandbox_image: str) -> None:
        """Mirrors `RuntimeChannel`'s own contract: a body naming a step the
        host never wired up fails like a denied effect, not a crashed
        conversation."""

        class _RefusingChannel:
            async def dispatch(self, call: EffectCall) -> EffectResult:
                return EffectResult(ok=False, error=f"no step named '{call.target}'")

        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source="async def flow(ctx, payload):\n    return await ctx.step('nope')\n",
                entrypoint="flow",
            ),
            run_id="unknown-step",
            input={},
            channel=_RefusingChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert not outcome.ok
        assert "nope" in outcome.error


class TestProtocolRobustness:
    async def test_print_does_not_corrupt_the_wire(self, sandbox_image: str) -> None:
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
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
            run_id="print-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "ok"


class TestCtxShimsInjected:
    async def test_a_shim_method_is_callable_from_the_body(self, sandbox_image: str) -> None:
        """A shim adding an entirely new method (rather than overriding one)
        proves ``ctx_shims`` reaches the running child, distinct from the
        override case PipesHub's ``pipeshub_tool`` shim exercises."""
        shim = (
            "\n"
            "    def double_locally(self, value):\n"
            "        return value * 2\n"
        )
        sandbox = DockerSandbox(sandbox_image, ctx_shims=shim)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def flow(ctx, payload):\n"
                    "    return ctx.double_locally(21)\n"
                ),
                entrypoint="flow",
            ),
            run_id="shim-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == 42

    async def test_a_shim_overriding_step_replaces_the_standard_one(
        self, sandbox_image: str
    ) -> None:
        """The shape PipesHub actually uses: a later `def step` in the class
        body replaces the earlier one, binding positional arguments a
        pre-migration source shape relies on."""
        shim = (
            "\n"
            "    async def step(self, target, *positional, name=None, **arguments):\n"
            "        if positional:\n"
            "            arguments = dict(zip(('a', 'b'), positional), **arguments)\n"
            "        return await self._call('step', target, arguments, 'write', name=name)\n"
        )
        sandbox = DockerSandbox(sandbox_image, ctx_shims=shim)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def flow(ctx, payload):\n"
                    "    return await ctx.step('bridge', 1, 2)\n"
                ),
                entrypoint="flow",
            ),
            run_id="shim-2",
            input={},
            channel=_RecordingChannel(replies={"bridge": "ok"}),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "ok"


class TestNoImageActionableError:
    async def test_a_bogus_image_names_the_build_command(self) -> None:
        bogus = f"loom-does-not-exist-{uuid.uuid4().hex}:latest"
        sandbox = DockerSandbox(bogus)

        error = await sandbox._ensure_image()

        assert error is not None
        assert bogus in error
        assert "docker build -t" in error
        assert "deployment/docker-sandbox" in error


class TestReadOnlyFilesystem:
    async def test_writes_outside_tmp_fail(self, sandbox_image: str) -> None:
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def scribble(ctx, payload):\n"
                    "    try:\n"
                    "        with open('/home/sandbox/should-fail.txt', 'w') as f:\n"
                    "            f.write('nope')\n"
                    "        return 'wrote'\n"
                    "    except OSError as exc:\n"
                    "        return 'blocked: %s' % exc\n"
                ),
                entrypoint="scribble",
            ),
            run_id="ro-fs-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value.startswith("blocked"), "a write outside /tmp succeeded"

    async def test_writes_inside_tmp_succeed(self, sandbox_image: str) -> None:
        """The counterpart: `--tmpfs /tmp` is writable, so a body staging a
        file for later `put_artifact`-style use is not blocked outright."""
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def scribble(ctx, payload):\n"
                    "    with open('/tmp/ok.txt', 'w') as f:\n"
                    "        f.write('fine')\n"
                    "    with open('/tmp/ok.txt') as f:\n"
                    "        return f.read()\n"
                ),
                entrypoint="scribble",
            ),
            run_id="ro-fs-2",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "fine"


class TestCapDropAll:
    async def test_the_effective_capability_set_is_empty(self, sandbox_image: str) -> None:
        """`--cap-drop ALL` zeroes `CapEff` regardless of the (already
        unprivileged) `sandbox` user this container runs as."""
        sandbox = DockerSandbox(sandbox_image)
        outcome = await sandbox.run(
            body=SandboxBody(
                invoke=_unused,
                source=(
                    "async def read_caps(ctx, payload):\n"
                    "    with open('/proc/self/status') as f:\n"
                    "        for line in f:\n"
                    "            if line.startswith('CapEff:'):\n"
                    "                return line.split()[1]\n"
                    "    return 'not-found'\n"
                ),
                entrypoint="read_caps",
            ),
            run_id="cap-1",
            input={},
            channel=_NoChannel(),
            policy=SandboxPolicy(max_wall_seconds=15),
        )

        assert outcome.ok, outcome.error
        assert outcome.value == "0000000000000000", (
            f"the container held capabilities: CapEff={outcome.value}"
        )


def test_the_module_source_is_readable() -> None:
    """Guards the suite itself, the same way `test_sandbox.py` does: every
    test above depends on this module's source being recoverable via
    `inspect.getsource` for `Runtime`-driven variants elsewhere in the suite."""
    import inspect
    import sys

    assert "class TestNetworkIsolation" in inspect.getsource(sys.modules[__name__])
