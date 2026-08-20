"""A workflow body in a child process, with every durable call proxied back.

The isolation is three things, and the sandbox declares which of them it can
actually apply on this platform rather than accepting a policy and ignoring
parts of it:

**A stripped environment.** An allowlist, not a denylist — a denylist has to
enumerate every secret name anybody will ever use and gets it wrong once. The
child sees ``allowed_env`` and nothing else, so a credential the host holds is
not readable by code the host did not write.

**POSIX rlimits.** Address space and CPU seconds, applied in the child before
any user code runs. Not available on Windows, which is why ``enforces`` is
computed rather than constant.

**A wall clock.** The one limit that is a timeout rather than a permission: a
body that never returns has to be given up on, or the host hangs.

The child holds no credentials and no store. Every ``ctx.*`` call is a line of
JSON on stdout; the parent dispatches it through the **same broker chain** an
inline run would use and writes the result back on stdin. That is what makes
grants, budgets, dry-run, and taint apply identically in both modes — a second
enforcement path inside the child would be a second thing to get wrong, and it
would be running on the untrusted side.
"""

from __future__ import annotations

import asyncio
import os
import resource
import sys
from typing import Any

from loom.runtime.sandbox import (
    ContextChannel,
    SandboxBody,
    SandboxOutcome,
    SandboxPolicy,
    unenforceable,
)
from loom.runtime.sandboxes._conversation import converse
from loom.runtime.sandboxes._harness import build_child_script

__all__ = ["SubprocessSandbox"]

#: Whether an address-space cap can actually be applied here.
#:
#: Darwin has ``RLIMIT_AS`` and refuses to set it to anything finite —
#: ``setrlimit`` raises ``ValueError: current limit exceeds maximum limit`` even
#: when *lowering* it, and the same goes for ``RLIMIT_DATA``. So ``hasattr`` is
#: not the question; whether the platform honours it is, and only macOS answers
#: differently from what its own API advertises. Probing for real is not an
#: option: the only conclusive probe is setting a finite limit, which would
#: permanently cap the host process that asked.
_MEMORY_LIMIT_SUPPORTED = hasattr(resource, "RLIMIT_AS") and sys.platform != "darwin"

#: `create_subprocess_exec` defaults `StreamReader`'s buffer to 64 KiB, so a
#: single tool result over that raises `LimitOverrunError` mid-conversation —
#: routine for a connector fetching a page of real data. 16 MiB comfortably
#: covers everything the wire protocol carries a line at a time.
_STREAM_LIMIT = 16 * 1024 * 1024

#: The standard harness, with no shims — see
#: :mod:`loom.runtime.sandboxes._harness` for what runs inside it and why it
#: is a string rather than a module.
_CHILD = build_child_script()


class SubprocessSandbox:
    """Runs a body in a credential-stripped child process."""

    name = "subprocess"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    @property
    def enforces(self) -> frozenset[str]:
        """What this platform can actually apply.

        Computed, not constant: rlimits are POSIX, and claiming a memory limit
        on Windows would leave a host believing in a bound that is not there.
        """
        applied = {"allowed_env", "max_wall_seconds", "allowed_imports"}
        if hasattr(resource, "RLIMIT_CPU"):
            applied.add("max_cpu_seconds")
        if _MEMORY_LIMIT_SUPPORTED:
            applied.add("max_memory_mb")
        # Deliberately absent: "network". A child process shares the host's
        # network namespace and nothing here can take it away, so a policy that
        # requires egress to be impossible is refused rather than accepted and
        # dropped. DockerSandbox is the tier that can honour it.
        return frozenset(applied)

    async def run(
        self,
        *,
        body: SandboxBody,
        run_id: str,
        input: Any,
        channel: ContextChannel,
        policy: SandboxPolicy | None = None,
    ) -> SandboxOutcome:
        """Execute *body* in a child, from its source rather than its callable.

        A function object cannot cross a process boundary, and pickling one
        would carry its closure — exactly the trusted state that must not go
        over. So this adapter reads :attr:`SandboxBody.source`, and refuses when
        the engine could not recover it: running *something else* because the
        real body was unavailable is the one outcome a sandbox must never have.
        """
        active = policy or self.policy

        unenforceable = self._unenforceable(active)
        if unenforceable:
            # Refusing beats running: a host that asked for a memory cap and
            # silently did not get one is worse off than one told it cannot
            # have it here, because it believes the untrusted code is bounded.
            # Same rule the check pipeline applies to a missing linter — a
            # check that cannot run has found nothing.
            return SandboxOutcome(
                ok=False,
                violation="policy",
                error=(
                    f"this sandbox cannot enforce {', '.join(unenforceable)} on "
                    f"{sys.platform}. It enforces {', '.join(sorted(self.enforces))}. "
                    "Drop the limit or run where it applies."
                ),
            )

        source, entrypoint = body.source, body.entrypoint
        if not source:
            return SandboxOutcome(
                ok=False,
                violation="source",
                error=(
                    "this sandbox runs a body from its source and none was "
                    "recovered for it. Publish the workflow with source= so the "
                    "version store holds it, or run it inline."
                ),
            )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated: no user site-packages, no PYTHON* env influence
            "-c",
            _CHILD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(active),
            preexec_fn=self._limits(active) if os.name == "posix" else None,
            limit=_STREAM_LIMIT,
        )

        try:
            return await asyncio.wait_for(
                converse(
                    process,
                    source,
                    entrypoint,
                    run_id,
                    input,
                    channel,
                    body.namespace,
                    allowed_imports=active.allowed_imports,
                ),
                timeout=active.max_wall_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return SandboxOutcome(
                ok=False,
                violation="max_wall_seconds",
                error=(
                    f"the body did not finish within {active.max_wall_seconds}s "
                    "and was killed"
                ),
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    # -- isolation ----------------------------------------------------------

    @staticmethod
    def _environment(policy: SandboxPolicy) -> dict[str, str]:
        """Only what the policy names, plus what Python needs to start.

        ``PATH`` and ``PYTHONHOME`` are not passed: ``-I`` already isolates the
        interpreter, and a body that needs to find executables is doing
        something a sandbox exists to stop.
        """
        allowed = {
            name: os.environ[name]
            for name in policy.allowed_env
            if name in os.environ
        }
        # Without this the child cannot import the stdlib on some layouts.
        for essential in ("SYSTEMROOT", "LD_LIBRARY_PATH"):
            if essential in os.environ:
                allowed.setdefault(essential, os.environ[essential])
        return allowed

    def _unenforceable(self, policy: SandboxPolicy) -> list[str]:
        """Limits this policy asks for that this platform will not apply.

        Derived from the policy rather than from a hand-kept list of two field
        names — which is how ``network`` and ``allowed_imports`` came to be
        accepted and silently ignored. See
        :func:`~loom.runtime.sandbox.unenforceable`.
        """
        return unenforceable(policy, self.enforces)

    @staticmethod
    def _limits(policy: SandboxPolicy) -> Any:
        """Applied in the child, before any user code runs.

        Nothing is caught here. An exception in ``preexec_fn`` aborts the spawn,
        which is the right failure: a limit that was declared enforceable and
        then could not be set means the sandbox is not the thing it claimed to
        be, and starting the body anyway would run untrusted code unbounded.
        ``run`` has already refused anything this platform is known not to
        honour, so reaching here with a bad limit is a genuine surprise.
        """

        def apply() -> None:
            if policy.max_memory_mb and _MEMORY_LIMIT_SUPPORTED:
                limit = policy.max_memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            if policy.max_cpu_seconds and hasattr(resource, "RLIMIT_CPU"):
                resource.setrlimit(
                    resource.RLIMIT_CPU,
                    (policy.max_cpu_seconds, policy.max_cpu_seconds),
                )

        return apply

    def __repr__(self) -> str:
        return f"<SubprocessSandbox enforces={sorted(self.enforces)}>"
