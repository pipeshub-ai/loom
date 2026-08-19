"""A workflow body in a Docker container, credentials- and network-free.

**Why Docker over `SubprocessSandbox`.** A bare child process shares this
host's network and filesystem, and on macOS has no enforceable memory limit
(`RLIMIT_AS` is refused there — see `loom.runtime.sandboxes.subprocess`'s own
`_MEMORY_LIMIT_SUPPORTED`). A container gives every one of those a real
boundary: `--network none`, a read-only root filesystem, and cgroup memory
accounting that works identically on every host OS Docker Desktop or a Linux
daemon runs on.

**Transport: `docker run --rm -i` over `asyncio.create_subprocess_exec`, not
`docker-py`'s `attach_socket`.** Plain stdin/stdout pipes make the
conversation loop byte-for-byte the shape
:func:`~loom.runtime.sandboxes._conversation.converse` already speaks to a
bare subprocess — no stream-demultiplexing framing (an attached socket
interleaves stdout/stderr behind 8-byte headers, and `tty=True` would
CRLF-mangle the JSON). It is also self-cleaning for the common crash case: if
this process dies, the `docker run` CLI process dies with it, the container's
stdin gets EOF, the child's `_await_reply` raises, the child exits, and `--rm`
removes the container — no orphan-reaping logic has to run for that path to
work.

**Resource enforcement is honest per limit** (see `enforces` below):
`--memory`/`--memory-swap` for `max_memory_mb` (cgroups, not `RLIMIT_AS`);
`ulimit -t` *inside* the container for `max_cpu_seconds` (containers are
always Linux, so `RLIMIT_CPU` applies regardless of the host OS — not
`--cpus`, which caps CPU *rate* and would silently misreport what
`max_cpu_seconds` promises); `asyncio.wait_for` + a `docker kill`/`docker rm
-f` backstop for `max_wall_seconds`; and `--network none` with `network=True`
refused outright, because a workflow body must never have direct egress —
every effect crosses the wire back to the parent's own channel.

**`ctx_shims`** lets a host extend the child's vocabulary (see
:mod:`loom.runtime.sandboxes._harness`) without forking this adapter — e.g. a
positional-argument compatibility shim for a bridge step whose calling
convention predates a host's own keyword-arguments migration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import uuid
from typing import Any

from loom.runtime.sandbox import ContextChannel, SandboxBody, SandboxOutcome, SandboxPolicy
from loom.runtime.sandboxes._conversation import converse
from loom.runtime.sandboxes._harness import build_child_script

__all__ = ["DockerSandbox"]

logger = logging.getLogger(__name__)

#: Mirrors `SubprocessSandbox`'s own `_STREAM_LIMIT`: `create_subprocess_exec`
#: defaults `readline()`'s buffer to 64 KiB, which a routine connector
#: payload exceeds. 16 MiB comfortably covers the wire protocol's largest
#: single line.
_STREAM_LIMIT = 16 * 1024 * 1024

_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")

#: `sweep_orphans` scopes to *this process's host*, not every sandbox
#: container in the cluster. Two hosts each running a sandbox share the
#: bare container label on a shared Docker daemon (a node pool, a dev
#: machine running two instances) — without a host-scoped label, host B's
#: startup sweep force-removes host A's in-flight run the moment it boots.
#: `HOSTNAME` is set by every container runtime (Kubernetes, Docker Compose,
#: plain `docker run`) and falls back to the PID for bare-metal processes
#: sharing no container identity at all.
_INSTANCE_ID = os.environ.get("HOSTNAME") or f"pid-{os.getpid()}"


class DockerSandbox:
    """Runs a body in a Docker container, credentials- and network-free."""

    name = "docker"

    #: Backstop only. `_remove_container` ordinarily stops on evidence -- the
    #: container appeared and was removed, or the `docker run` process exited
    #: without creating one -- and this bounds the case where a wedged daemon
    #: supplies neither.
    _REMOVE_DEADLINE_SECONDS = 30.0
    _REMOVE_POLL_SECONDS = 0.1

    def __init__(
        self,
        image: str,
        *,
        docker_binary: str = "docker",
        ctx_shims: str = "",
        container_label: str = "loom.workflow-sandbox",
    ) -> None:
        self.image = image
        self._docker = docker_binary
        self._child = build_child_script(ctx_shims=ctx_shims)
        self._container_label_key = container_label
        self._container_label = f"{container_label}=1"
        self._instance_label_key = f"{container_label}-instance"
        self._instance_label = f"{self._instance_label_key}={_INSTANCE_ID}"

    @property
    def enforces(self) -> frozenset[str]:
        """Every field `SandboxPolicy` declares. Unlike `SubprocessSandbox`
        (platform-conditional on `resource.RLIMIT_*`), a container is always
        Linux from the inside, so every limit applies unconditionally
        regardless of the host this process itself runs on."""
        return frozenset(
            {"allowed_env", "max_wall_seconds", "max_memory_mb", "max_cpu_seconds", "network"}
        )

    async def run(
        self,
        *,
        body: SandboxBody,
        run_id: str,
        input: Any,
        channel: ContextChannel,
        policy: SandboxPolicy | None = None,
    ) -> SandboxOutcome:
        active = policy or SandboxPolicy()

        if active.network:
            return SandboxOutcome(
                ok=False,
                violation="policy",
                error=(
                    "this sandbox never grants container network access -- "
                    "every effect must go through the channel back to the "
                    "parent. Drop network=True from the SandboxPolicy."
                ),
            )

        source, entrypoint = body.source, body.entrypoint
        if not source:
            return SandboxOutcome(
                ok=False,
                violation="source",
                error=(
                    "this sandbox runs a body from its source and none was "
                    "recovered for it. Publish the workflow with source= so "
                    "the version store holds it, or run it inline."
                ),
            )

        image_error = await self._ensure_image()
        if image_error:
            return SandboxOutcome(ok=False, error=image_error)

        container_name = self._container_name(run_id)
        command = self._docker_command(container_name, active)

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )

        try:
            return await asyncio.wait_for(
                converse(
                    process, source, entrypoint, run_id, input, channel, body.namespace
                ),
                timeout=active.max_wall_seconds,
            )
        except TimeoutError:
            await self._kill(container_name, process)
            return SandboxOutcome(
                ok=False,
                violation="max_wall_seconds",
                error=(
                    f"the body did not finish within {active.max_wall_seconds}s "
                    "and its container was killed"
                ),
            )
        finally:
            # Control-flow exceptions (`Suspend`, `WorkflowCancelled`) from
            # `channel.dispatch` inside `converse` reach here too -- the
            # engine requires them to propagate untouched, and this `finally`
            # is what still guarantees the container is gone on that path.
            if process.returncode is None:
                await self._kill(container_name, process)

    # -- container lifecycle -------------------------------------------------

    async def _ensure_image(self) -> str | None:
        """`None` when the image is available; otherwise an error message
        naming the build command."""
        check = await asyncio.create_subprocess_exec(
            self._docker, "image", "inspect", self.image,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await check.wait()
        if check.returncode == 0:
            return None

        logger.info("sandbox image %s not found locally, attempting pull ...", self.image)
        pull = await asyncio.create_subprocess_exec(
            self._docker, "pull", self.image,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await pull.communicate()
        if pull.returncode == 0:
            return None

        detail = stderr.decode(errors="replace").strip()
        logger.error("failed to pull sandbox image %s: %s", self.image, detail)
        return (
            f"Sandbox image '{self.image}' is not available locally and "
            f"could not be pulled ({detail}). Build it with:\n"
            f"    docker build -t {self.image} deployment/docker-sandbox\n"
            "or point this DockerSandbox at an image you have pushed to "
            "your own registry."
        )

    def _container_name(self, run_id: str) -> str:
        """`loom-sbx-{run_id}-{nonce}`: deterministic enough to find (the
        orphan reaper matches on the label, not this name), unique enough
        that a retried dispatch of the same run never collides with a
        container still winding down from a previous one."""
        safe = _NAME_UNSAFE.sub("-", run_id).strip("-") or "run"
        return f"loom-sbx-{safe}-{uuid.uuid4().hex[:8]}"

    def _docker_command(self, container_name: str, policy: SandboxPolicy) -> list[str]:
        command = [
            self._docker, "run", "--rm", "-i", "--init",
            "--label", self._container_label,
            "--label", self._instance_label,
            "--name", container_name,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=50m",
            "--user", "sandbox",
            # Defence in depth beyond `--network none`: a generated body has
            # no legitimate reason to gain privileges, hold any Linux
            # capability (raw sockets, ptrace, module loading), or fork
            # enough processes to exhaust the host's PID table.
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--pids-limit", "128",
        ]
        if policy.max_memory_mb:
            memory = f"{policy.max_memory_mb}m"
            command += ["--memory", memory, "--memory-swap", memory]
        for name, value in self._environment(policy).items():
            command += ["-e", f"{name}={value}"]
        command.append(self.image)
        command += self._container_entrypoint(policy)
        return command

    @staticmethod
    def _environment(policy: SandboxPolicy) -> dict[str, str]:
        """Only what the policy names -- an allowlist, not a denylist, the
        same rule `SubprocessSandbox._environment` applies. Unlike that
        adapter, nothing is added for "what Python needs to start": the
        image's own `python3` needs no host env at all."""
        return {name: os.environ[name] for name in policy.allowed_env if name in os.environ}

    def _container_entrypoint(self, policy: SandboxPolicy) -> list[str]:
        """`ulimit -t` applied inside the container, before `python3` starts.

        The child script is passed as a distinct argv element (`"$2"`/`"$1"`
        expanded by the shell from a positional parameter), never
        interpolated into the `-c` string itself -- the script contains
        quotes, backslashes, and newlines, and building a shell string
        around it would mean re-deriving shell quoting rules for arbitrary
        Python source. `sh -c 'CMD' sh arg1 arg2` binds `arg1` to `$1` and
        so on; `sh` as `$0` is conventional and unused here.
        """
        if policy.max_cpu_seconds:
            return [
                "sh", "-c", 'ulimit -t "$1"; exec python3 -I -c "$2"',
                "sh", str(policy.max_cpu_seconds), self._child,
            ]
        return ["sh", "-c", 'exec python3 -I -c "$1"', "sh", self._child]

    async def _kill(self, container_name: str, process: Any) -> None:
        """Remove the container, *then* kill the `docker run` CLI.

        That order is the whole point. Killing the CLI first is a race it
        loses under load: SIGKILL does not cancel a create the daemon has
        already accepted, so a remove issued after it finds no such name, and
        the container is created *afterwards* -- landing in `Created`, which
        `--rm` never covers because the container never ran. A short
        `max_wall_seconds` is exactly the window that makes the CLI likeliest
        to die mid-create, so the wall-timeout path leaked a container in most
        runs under load while passing every time it was run alone.

        Leaving the CLI alive is what makes the removal converge: the create
        completes as it normally would, and removing the container is itself
        what makes `docker run` exit, so the kill below is the backstop it was
        always meant to be rather than the mechanism.
        """
        await self._remove_container(container_name, process)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    async def _remove_container(self, container_name: str, process: Any) -> None:
        """Wait for the container to exist, force-remove it, confirm it is
        gone.

        The waiting is what the daemon's create latency requires, and the
        confirming is what `docker rm` requires: **`docker rm -f` exits 0 for
        a container that does not exist**, so its status says only that the
        daemon answered, never that anything was removed. Reading it as proof
        is how a removal that ran too early reports success and the container
        appears a second later.

        What bounds the wait is the `docker run` process, not a duration.
        A create the daemon has accepted lands eventually and no fixed
        deadline is right for "eventually" -- a ten-second one still leaked
        under load, because a saturated daemon took longer. But the CLI
        cannot exit before its container exists, so *the CLI having exited
        with no container* is positive evidence that none is coming, which a
        timer can only ever guess at. `_REMOVE_DEADLINE_SECONDS` stays as a
        backstop for a daemon wedged badly enough to do neither.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._REMOVE_DEADLINE_SECONDS
        while True:
            if await self._exists(container_name):
                await self._remove(container_name)
                if not await self._exists(container_name):
                    return
            elif process.returncode is not None:
                # `docker run` is gone and created nothing -- it failed before
                # the create, or its container ran and `--rm` took it. Either
                # way nothing is coming, and this is the ordinary exit.
                return
            if loop.time() >= deadline:
                logger.warning(
                    "sandbox container %s was neither created nor removed "
                    "within %.1fs; leaving it to the next orphan sweep",
                    container_name, self._REMOVE_DEADLINE_SECONDS,
                )
                return
            await asyncio.sleep(self._REMOVE_POLL_SECONDS)

    async def _exists(self, container_name: str) -> bool:
        """Whether a container by that exact name is known to the daemon,
        `Created` and never started included."""
        lookup = await asyncio.create_subprocess_exec(
            self._docker, "ps", "-aq", "--filter", f"name=^{container_name}$",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await lookup.communicate()
        return bool(stdout.strip())

    async def _remove(self, container_name: str) -> None:
        """`docker rm -f` one container. The exit status is deliberately
        ignored -- see `_remove_container`."""
        remover = await asyncio.create_subprocess_exec(
            self._docker, "rm", "-f", container_name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        with contextlib.suppress(Exception):
            await remover.wait()

    async def sweep_orphans(self) -> int:
        """Force-remove every container *this instance* left running, at
        startup.

        Covers the one crash path `--rm` does not: a process that is
        SIGKILLed leaves its `docker run` CLI dead, but a container whose
        child never returns to reading stdin (an infinite loop in generated
        code, wall-timeout notwithstanding if the sweep runs before that
        fires) keeps running under Docker's own lifetime, not this
        process's. Best-effort -- a daemon that is unreachable here will
        also fail every subsequent `run()`, so this does not raise.

        Filtered on the instance label, not just the container label: on a
        Docker daemon shared by multiple replicas (a node pool, or two
        processes on one dev machine), a bare container-label filter would
        match every replica's containers and a process restarting on boot
        would force-remove another replica's in-flight run.

        Also swallows a missing `docker` binary (`OSError`/`FileNotFoundError`
        from the exec itself, distinct from the daemon-unreachable case above,
        which still surfaces as a non-zero return code): this typically runs
        once, at `Runtime` construction, and letting it raise there would take
        down the entire host process over a sandbox feature nothing had tried
        to use yet. The first real sandboxed run reports the same problem on
        its own exec, where it can be attributed to that run instead of to
        startup.
        """
        try:
            list_proc = await asyncio.create_subprocess_exec(
                self._docker, "ps", "-aq",
                "--filter", f"label={self._container_label}",
                "--filter", f"label={self._instance_label}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.warning("sandbox orphan sweep could not launch docker: %s", exc)
            return 0
        stdout, stderr = await list_proc.communicate()
        if list_proc.returncode != 0:
            logger.warning(
                "sandbox orphan sweep could not list containers: %s",
                stderr.decode(errors="replace").strip(),
            )
            return 0

        ids = [line for line in stdout.decode().splitlines() if line.strip()]
        if not ids:
            return 0

        logger.warning(
            "sandbox orphan sweep: force-removing %d stale container(s) "
            "labeled %s, %s", len(ids), self._container_label, self._instance_label,
        )
        remove_proc = await asyncio.create_subprocess_exec(
            self._docker, "rm", "-f", *ids,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await remove_proc.communicate()
        if remove_proc.returncode != 0:
            logger.warning(
                "sandbox orphan sweep: some containers could not be "
                "removed: %s", stderr.decode(errors="replace").strip(),
            )
        return len(ids)

    def __repr__(self) -> str:
        return f"<DockerSandbox image={self.image!r}>"
