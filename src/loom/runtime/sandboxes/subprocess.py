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
import json
import os
import resource
import sys
import textwrap
from typing import Any

from loom.core.serde import encode
from loom.runtime.effects import EffectCall
from loom.runtime.sandbox import (
    ContextChannel,
    SandboxBody,
    SandboxOutcome,
    SandboxPolicy,
)
from loom.toolsets.manifest import EffectClass

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

#: Runs inside the child. A string rather than a module so the child imports
#: nothing of Loom's — it needs no store, no credentials, and no engine, and
#: giving it any of those would put trusted code on the untrusted side.
_CHILD = textwrap.dedent('''\
    import asyncio, json, sys

    # The real stdout, captured before user source can touch `sys.stdout`. A
    # body that calls `print()` (routine in model-generated code) would
    # otherwise write straight into the JSON protocol channel and corrupt
    # every message after it.
    _wire_out = sys.stdout

    def _emit(message):
        _wire_out.write(json.dumps(message) + "\\n")
        _wire_out.flush()

    def _await_reply():
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("the host closed the channel")
        return json.loads(line)

    class Ctx:
        """Every durable call becomes a line on stdout.

        No journal, no store, no credentials here. The parent owns all of it,
        which is the point: this process is the untrusted side.
        """

        def __init__(self, run_id):
            self.run_id = run_id

        @staticmethod
        def _named(target):
            """A step's name, however the body referred to it.

            A body writes ``ctx.step(charge, ...)`` and gets a StepDefinition,
            which cannot cross the pipe. Only the name has to: the parent holds
            the implementations, and sending anything more would be sending the
            untrusted side a handle on trusted code.
            """
            return getattr(target, "name", None) or getattr(
                target, "__name__", None
            ) or str(target)

        async def _call(self, kind, target, arguments, effect, *, name=None):
            message = {"t": "call", "kind": kind, "target": self._named(target),
                       "arguments": arguments, "effect": effect}
            if name is not None:
                message["name"] = name
            _emit(message)
            reply = _await_reply()
            if not reply.get("ok", False):
                raise RuntimeError(reply.get("error") or "refused")
            return reply.get("value")

        async def step(self, target, *positional, name=None, **arguments):
            if positional:
                # The wire carries a named mapping, and guessing parameter
                # names here would bind arguments to the wrong ones silently.
                raise TypeError(
                    "a sandboxed body must pass step arguments by keyword; "
                    "got %d positional" % len(positional)
                )
            return await self._call("step", target, arguments, "write", name=name)

        async def tool(self, target, **arguments):
            return await self._call("tool", target, arguments, "write")

        async def read(self, target, **arguments):
            return await self._call("tool", target, arguments, "read")

        async def agent(self, prompt, **arguments):
            return await self._call("agent", prompt, arguments, "write")

        async def node(self, node_id, payload=None, **arguments):
            """Call a catalogued node. The payload crosses as plain JSON.

            A Pydantic model cannot go over the pipe, and ``ctx.node`` validates
            a mapping into the node's own Input on the parent side anyway — so
            the model is built where the type is known, which is the trusted
            side.
            """
            body = payload
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json")
            return await self._call(
                "node", node_id, dict(arguments, payload=body), "write"
            )

        async def wait_for_event(self, name, **arguments):
            """Park until something outside answers.

            The call dies with this process — the parent raises Suspend and the
            child goes with it. Nothing is lost: the child holds no durable
            state, so re-entry re-runs the body from the top with every earlier
            call served from the parent's journal, and this one returns the
            recorded answer.
            """
            return await self._call("event", name, arguments, "read")

        async def wait_for_approval(self, subject, **arguments):
            return await self._call(
                "event", "approval:" + str(subject), arguments, "read"
            )

        async def sleep(self, seconds, *, name="sleep"):
            """Durably pause. Dies with this process like any other suspend:
            the parent's `Suspend` propagates out, the child exits, and
            re-entry resumes from the journaled wake time."""
            return await self._call(
                "sleep", "sleep", {"seconds": seconds, "name": name}, "write"
            )

        async def report(self, message, **arguments):
            """Not durable — mirrors `Context.report`, which is not journaled
            either. Reaches the parent's run stream and nothing else."""
            return await self._call("report", message, arguments, "read")

    def _find_entrypoint(namespace, name):
        """The workflow to run: by name, else the sole workflow-like object.

        A host that renamed the definition (PipesHub compiles every version
        under a synthetic name so its `code_hash` is unique) binds it in the
        exec'd namespace under the *original* function name, not the one the
        engine sent as ``entrypoint`` — so the direct lookup misses. Falling
        back to "the only thing here that looks like a workflow" is safe
        specifically because the host's own compiler already enforces exactly
        one `@workflow` per source; if that ever stops being true, refusing
        rather than guessing which of several to run is the correct failure.
        """
        entry = namespace.get(name)
        if entry is not None:
            return entry
        candidates = [
            value
            for value in namespace.values()
            # `hasattr(value, "triggers")` is what tells a WorkflowDefinition
            # from a StepDefinition: both wrap a callable under `.fn`, but
            # only a workflow carries trigger specs. Without this a module
            # exporting local `@step` helpers alongside its one `@workflow`
            # (the ordinary shape) would see more than one "candidate" and
            # refuse to fall back at all.
            if callable(value) and hasattr(value, "fn") and hasattr(value, "triggers")
        ]
        if len(candidates) == 1:
            return candidates[0]
        raise RuntimeError(
            "no %r in the sandboxed source, and %d workflow-like candidates "
            "were found (need exactly 1 to fall back)" % (name, len(candidates))
        )

    async def _main():
        request = json.loads(sys.stdin.readline())
        namespace = dict(request.get("namespace") or {})
        # User source runs with stdout redirected to stderr: only `_emit`
        # (bound to the real stdout above) may write to the wire.
        sys.stdout = sys.stderr
        exec(compile(request["source"], "<sandboxed>", "exec"), namespace)
        entry = _find_entrypoint(namespace, request["entrypoint"])
        result = entry(Ctx(request["run_id"]), request["input"])
        if hasattr(result, "__await__"):
            result = await result
        _emit({"t": "done", "value": result})

    try:
        asyncio.run(_main())
    except BaseException as exc:
        _emit({"t": "error", "error": "%s: %s" % (type(exc).__name__, exc)})
        sys.exit(1)
''')


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
        applied = {"allowed_env", "max_wall_seconds"}
        if hasattr(resource, "RLIMIT_CPU"):
            applied.add("max_cpu_seconds")
        if _MEMORY_LIMIT_SUPPORTED:
            applied.add("max_memory_mb")
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
                self._converse(
                    process, source, entrypoint, run_id, input, channel, body.namespace
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

    # -- the conversation ---------------------------------------------------

    async def _converse(
        self,
        process: Any,
        source: str,
        entrypoint: str,
        run_id: str,
        input: Any,
        channel: ContextChannel,
        namespace: dict[str, Any] | None = None,
    ) -> SandboxOutcome:
        process.stdin.write(
            (
                json.dumps(
                    {"source": source, "entrypoint": entrypoint,
                     "run_id": run_id, "input": input,
                     "namespace": namespace or {}}
                )
                + "\n"
            ).encode()
        )
        await process.stdin.drain()

        calls = 0
        while True:
            line = await process.stdout.readline()
            if not line:
                stderr = (await process.stderr.read()).decode().strip()
                return SandboxOutcome(
                    ok=False,
                    error=(
                        stderr.splitlines()[-1]
                        if stderr
                        else "the sandbox exited without reporting"
                    ),
                )

            message = json.loads(line)
            kind = message.get("t")
            if kind == "done":
                return SandboxOutcome(ok=True, value=message.get("value"), calls=calls)
            if kind == "error":
                return SandboxOutcome(
                    ok=False, error=message.get("error", ""), calls=calls
                )

            calls += 1
            # A dispatch that parks the run raises straight through here, and
            # the child dies with it. That is correct rather than lossy: the
            # child holds no durable state, so re-entry after the event arrives
            # re-executes the body from the top with every earlier call served
            # from the parent's journal — deterministic re-entry is what the
            # engine already relies on, and a sandbox that tried to preserve a
            # suspended child instead would be inventing a second one.
            outcome = await channel.dispatch(
                EffectCall(
                    kind=message["kind"],
                    target=message["target"],
                    arguments=message.get("arguments") or {},
                    effect=EffectClass(message.get("effect", "write")),
                    run_id=run_id,
                    name=message.get("name"),
                )
            )
            process.stdin.write(
                (
                    json.dumps(
                        {
                            "ok": outcome.ok,
                            # Encoded, not passed through: a step or node returns
                            # Pydantic models, dataclasses, and Attachments, none
                            # of which are JSON. The child gets the JSON shape
                            # rather than the reconstructed object — rebuilding
                            # one there would mean importing the parent's types
                            # into the untrusted process, which is the thing this
                            # sandbox exists to avoid.
                            "value": encode(outcome.value),
                            "error": outcome.error,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()

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
        """Limits this policy asks for that this platform will not apply."""
        asked = {
            "max_memory_mb": policy.max_memory_mb,
            "max_cpu_seconds": policy.max_cpu_seconds,
        }
        return sorted(
            name for name, value in asked.items() if value and name not in self.enforces
        )

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
