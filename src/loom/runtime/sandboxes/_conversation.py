"""The JSON-lines conversation loop shared by every out-of-process sandbox.

Once a child (a bare process, a container) is running the script
:mod:`~loom.runtime.sandboxes._harness` builds, the protocol from here on is
identical regardless of what is hosting that child: one bootstrap line out,
then a `{"t": "call", ...}` / reply exchange per durable operation, ending in
`{"t": "done"}` or `{"t": "error"}`. :class:`~loom.runtime.sandboxes.subprocess.SubprocessSandbox`
and :class:`~loom.runtime.sandboxes.docker.DockerSandbox` differ in how the
process comes to exist and what bounds it while it runs — not in how they talk
to it once it does. Keeping that one loop in one place is what keeps the two
adapters from silently drifting apart on the wire format a change to either
would have to keep compatible by hand.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from loom.core.serde import encode
from loom.runtime.effects import EffectCall
from loom.runtime.sandbox import ContextChannel, RuntimeChannel, SandboxOutcome
from loom.toolsets.manifest import EffectClass

__all__ = ["converse"]


class _Streams(Protocol):
    """The half of `asyncio.subprocess.Process` this loop actually uses.

    Named narrowly rather than importing `asyncio.subprocess.Process` as the
    parameter type: a Docker adapter's process is exactly that type too, so
    this is documentation of the real contract, not a second one.
    """

    stdin: Any
    stdout: Any
    stderr: Any


async def converse(
    process: _Streams,
    source: str,
    entrypoint: str,
    run_id: str,
    input: Any,
    channel: ContextChannel,
    namespace: dict[str, Any] | None = None,
    allowed_imports: frozenset[str] | None = None,
) -> SandboxOutcome:
    """Speak the wire protocol to an already-running child.

    Sends the bootstrap message, then loops: read a line, dispatch it through
    *channel* if it is a call, write the encoded reply, repeat — until a
    `"done"`/`"error"` terminal message or the pipe closes. A dispatch that
    parks the run raises straight through here and the caller's `finally`
    is what cleans up the child; this function does not catch it.
    """
    process.stdin.write(
        (
            json.dumps(
                {
                    "source": source, "entrypoint": entrypoint,
                    "run_id": run_id, "input": input,
                    "namespace": namespace or {},
                    # `None` rather than an empty list when there is no policy:
                    # the child distinguishes "no import policy" from "a policy
                    # permitting nothing", and a list would collapse the two.
                    "allowed_imports": (
                        None if allowed_imports is None else sorted(allowed_imports)
                    ),
                }
            )
            + "\n"
        ).encode()
    )
    await process.stdin.drain()

    async def run_child_local(call: EffectCall) -> Any:
        """Ask the child to run a ``@step`` it already exec'd.

        Written while ``channel.dispatch`` is blocked inside ``ctx.step``,
        so this is the only reader of stdout until the child sends
        ``delegated_result``. The child's first ``_await_reply`` is waiting
        on this ``delegate`` line — not a deadlock, the same pipe in the
        other direction.
        """
        process.stdin.write(
            (json.dumps({"ok": True, "delegate": True}) + "\n").encode()
        )
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise RuntimeError("the child exited while running a local step")
        message = json.loads(line)
        if message.get("t") != "delegated_result":
            raise RuntimeError(
                "expected delegated_result from the child, got "
                f"{message.get('t')!r}: {message.get('error') or message}"
            )
        if not message.get("ok", True):
            raise RuntimeError(message.get("error") or "local step failed")
        return message.get("value")

    if isinstance(channel, RuntimeChannel):
        channel.child_local = run_child_local

    calls = 0
    while True:
        line = await process.stdout.readline()
        if not line:
            stderr = (await process.stderr.read()).decode(errors="replace").strip()
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
                local=bool(message.get("local")),
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
                        # sandbox exists to avoid. ``ctx.agent`` is the
                        # exception that lives in the child harness: it wraps
                        # the dict in a tiny duck type so ``result.text()``
                        # matches the inline ``AgentResult`` the prompt
                        # teaches, without importing loom.agents.
                        "value": encode(outcome.value),
                        "error": outcome.error,
                    }
                )
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
