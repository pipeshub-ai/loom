"""Smoke-run generated workflow code before handing it over.

AST validation catches code that is malformed. It says nothing about code that
parses cleanly and then fails on the first line — a misspelled import, a step
called with the wrong arity, a workflow body that touches ``ctx`` incorrectly.
Those are the failures a user actually hits, and they are cheap to catch by
running the thing once.

The run happens in a subprocess against ``MemoryStore`` and a
``MockModelProvider``, so it needs no network, no API key, and no infrastructure
— and a generated workflow that hangs or exits cannot take the agent with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Wall-clock ceiling for one smoke run. Generated workflows are small; anything
#: slower is a loop, and a hung subprocess is worse than a failed check.
DEFAULT_TIMEOUT_SECONDS = 30.0


#: Failures that are about the machine rather than the code — no credential, no
#: network, no service. Named once because three places need the distinction
#: and each got it slightly wrong on its own: the smoke stage fed 401s into the
#: repair loop until a workflow came back gutted, and ``generate`` told callers
#: to raise ``max_discovery_turns`` when the real answer was to set an API key.
ENVIRONMENTAL_MARKERS: tuple[str, ...] = (
                "401", "403", "unauthorized", "forbidden",
                "invalid authentication", "authentication_error",
                "insufficient authentication scopes", "insufficient permissions",
                "credential", "api key", "api_key", "access token",
                "invalid_grant", "no google credentials",
                "connection refused", "connecterror", "connecttimeout",
                "getaddrinfo", "name or service not known",
                "temporary failure in name resolution", "ssl", "timed out",
            )


def is_environmental(text: str) -> bool:
    """True when *text* describes a missing credential, service, or network.

    A missing import or a bad signature is deliberately **not** environmental:
    those are the failures the checks exist to catch, and they stay repairable.
    """
    haystack = text.lower()
    if "no module named" in haystack or "cannot import name" in haystack:
        return False
    return any(marker in haystack for marker in ENVIRONMENTAL_MARKERS)


#: Environment variables the smoke child is allowed to see. An allowlist, not a
#: denylist, for the reason `SandboxPolicy.allowed_env` gives: a denylist has to
#: enumerate every secret name anybody will ever use and gets it wrong once.
#:
#: Everything here is something the *interpreter* needs in order to start and
#: find its own packages. Nothing here is a credential. In particular
#: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, every `LOOM_*` var, and every cloud
#: credential are absent — which is what makes the claim "this runs with no
#: credentials" true rather than aspirational.
DEFAULT_SMOKE_ENV: frozenset[str] = frozenset({
    "PATH",
    # Passed through deliberately, and the reason `-I` is *not* used on the
    # child: an editable or monorepo install resolves `loom` through
    # PYTHONPATH, and an interpreter told to ignore it would smoke-test against
    # a different copy of the SDK than the one the caller is running.
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "LANG",
    "LC_ALL",
    "TZ",
})


@dataclass(frozen=True)
class SmokeIsolation:
    """What the smoke child may reach.

    The smoke stage executes code a *model* wrote. It ran with the host's
    entire environment inherited — every API key the process held — while the
    tool that exposes it told the model "no real network or credentials". The
    allowlist below is what closes the credential half of that gap; the network
    half needs a real sandbox, which is what `sandbox` is for.

    Separated from `smoke_run` rather than being a pile of keyword arguments so
    that a host has one object to override and one place to read what the
    current policy actually is.
    """

    env_allowlist: frozenset[str] = DEFAULT_SMOKE_ENV
    """Variable names passed through to the child. Everything else is stripped."""

    inherit_env: bool = False
    """Escape hatch. ``True`` restores the pre-allowlist behaviour for a host
    that has deliberately decided the code it smoke-tests is trusted. Off by
    default, because the default caller is a coding agent."""

    def environment(self) -> dict[str, str]:
        """The child's environment, built from this process's."""
        import os

        if self.inherit_env:
            return dict(os.environ)
        return {
            name: os.environ[name]
            for name in self.env_allowlist
            if name in os.environ
        }

    def describe(self) -> str:
        """One line, for a tool description that must not overclaim."""
        if self.inherit_env:
            return "inherits the host environment (credentials included)"
        return "no host credentials; network is not restricted"


DEFAULT_ISOLATION = SmokeIsolation()


@dataclass
class SmokeResult:
    """Outcome of executing generated code once."""

    ok: bool
    phase: str
    """Where it got to: ``compile``, ``import``, ``run``, or ``done``."""
    error: str = ""
    """The exception message, ready to hand back to the model."""
    traceback: str = ""
    status: str = ""
    """Terminal status of the run, when one completed."""
    steps_executed: int = 0
    synthetic_input: bool = False
    """The input was derived from the workflow's declared type, not supplied."""
    """Journal entries the smoke run actually produced.

    The difference between "the code ran" and "the code was started". A workflow
    whose first act is a long ``ctx.sleep`` parks before any step executes, and a
    run that executed nothing has demonstrated nothing about the code inside it.
    """
    output_preview: str = ""
    empty_paths: list[str] = field(default_factory=list)
    """Paths in the output holding an empty collection, e.g. ``stage2.fields``.

    Computed in the runner, where the output is whole. ``output_preview`` is
    capped at 400 characters, so a nested empty collection inside a large
    result is not recoverable from it — which is exactly the shape the real
    failure had."""
    workflows_found: list[str] = field(default_factory=list)

    @property
    def unverifiable(self) -> bool:
        """True when *our* invented input caused the failure, not the code.

        A workflow annotated ``input_data: dict`` gets ``{}`` from the schema
        faker — there are no keys to invent — and a body that reads
        ``input_data["url"]` then fails with ``KeyError: 'url'``. The code is
        right; the harness could not supply an input it could run against.

        Treating that as a defect is the same mistake as feeding a 401 to the
        repair loop: it asks the model to fix something that is not broken, and
        the cheapest way to satisfy it is to delete the input handling.

        Narrow on purpose. It applies only when the input was *synthesised*, so
        a caller who passed ``smoke_input`` still gets a genuine failure
        reported as one.
        """
        if self.ok or not self.synthetic_input:
            return False
        return any(
            marker in (self.error or "")
            for marker in ("KeyError", "IndexError", "'", '"')
        ) and self.steps_executed == 0

    @property
    def environmental(self) -> bool:
        """True when the run failed for a reason outside the generated code.

        The smoke sandbox has no credentials and no network by design, so a
        workflow that talks to a real service fails there however well it is
        written. Feeding that back as "your code failed, fix it" asks the model
        to repair something that is not broken, and the cheapest way to satisfy
        it is to delete the integration — which is exactly what happens: the
        workflow comes back gutted, passing smoke because it no longer does
        anything, and marked clean.

        A missing import or a bad signature is *not* environmental. Those are
        the failures this check exists to catch, and they stay repairable.
        """
        if self.ok:
            return False
        haystack = f"{self.error}\n{self.traceback}".lower()
        if "no module named" in haystack or "cannot import name" in haystack:
            return False
        return is_environmental(haystack)

    def as_feedback(self, code: str = "") -> str:
        """Phrase the failure as a repair instruction for the coding agent.

        Pass the *code*. The coding agent is ephemeral — each call starts a fresh
        conversation — so a repair round that sends only the traceback asks the
        model to fix something it cannot see, and it will happily invent a reply.
        """
        if self.ok:
            return "Smoke run passed: the workflow compiled and executed."

        detail = self.traceback or self.error
        parts = [
            f"The workflow you generated failed during {self.phase}. Return the "
            "complete corrected file.",
            f"\n## Error\n\n{detail}",
        ]
        if code:
            parts.append(f"\n## The code that failed\n\n```python\n{code}\n```")
        return "\n".join(parts)


def compile_check(code: str) -> SmokeResult:
    """Compile without executing. Catches what ``ast.parse`` lets through.

    ``ast.parse`` accepts some source that ``compile`` rejects — for example a
    ``return`` outside a function.
    """
    try:
        compile(code, "<generated>", "exec")
    except (SyntaxError, ValueError) as exc:
        return SmokeResult(ok=False, phase="compile", error=str(exc))
    return SmokeResult(ok=True, phase="compile")


def smoke_run(
    code: str,
    workflow_input: Any = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
    fakes: list[tuple[str, str]] | None = None,
    isolation: SmokeIsolation | None = None,
) -> SmokeResult:
    """Compile, import, and execute *code* once in a subprocess.

    Parameters
    ----------
    workflow_input:
        Input passed to the discovered workflow. Must be JSON-serializable,
        since it crosses a process boundary.
    timeout:
        Seconds before the subprocess is killed and the run reported as failed.
    python:
        Interpreter to use. Defaults to the current one, so the smoke run sees
        exactly the packages the caller has.

    fakes:
        ``(tools_module, manifest_import_path)`` pairs whose operations are
        replaced with stand-ins before the code runs. Without them a workflow
        that talks to a real service can only reach an authentication failure
        here, which proves nothing about the code.

    isolation:
        What the child may reach. Defaults to :data:`DEFAULT_ISOLATION`, which
        strips every environment variable that is not needed to start a Python
        interpreter — so the code being tested cannot read the credentials the
        host happens to hold.

    Never raises for a *generated code* problem — a failure is a
    :class:`SmokeResult`, because the caller's job is to feed it back to the
    model rather than to crash.
    """
    compiled = compile_check(code)
    if not compiled.ok:
        return compiled

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / "generated_flow.py").write_text(code, encoding="utf-8")
        (workspace / "_runner.py").write_text(_RUNNER, encoding="utf-8")

        try:
            # Fixed argv, no shell — the generated code is a file argument,
            # never interpolated into a command line.
            completed = subprocess.run(
                [
                    python or sys.executable,
                    "_runner.py",
                    json.dumps(workflow_input),
                    json.dumps(fakes or []),
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                # Never the inherited environment. See SmokeIsolation.
                env=(isolation or DEFAULT_ISOLATION).environment(),
            )
        except subprocess.TimeoutExpired:
            return SmokeResult(
                ok=False,
                phase="run",
                error=(
                    f"the workflow did not finish within {timeout:.0f}s — it is "
                    "probably looping, or waiting on something that never arrives"
                ),
            )

    return _parse_runner_output(completed.stdout, completed.stderr)


def _parse_runner_output(stdout: str, stderr: str) -> SmokeResult:
    """Read the runner's JSON verdict, falling back to raw output."""
    for line in reversed(stdout.splitlines()):
        if not line.startswith("__LOOM_SMOKE__"):
            continue
        payload = json.loads(line.removeprefix("__LOOM_SMOKE__"))
        return SmokeResult(**payload)

    # The subprocess died before reporting — surface whatever it managed to say.
    return SmokeResult(
        ok=False,
        phase="import",
        error=(stderr.strip().splitlines() or ["subprocess produced no output"])[-1],
        traceback=stderr.strip(),
    )


#: Executed inside the subprocess. Kept as a string rather than a module so the
#: temporary workspace is self-contained and importable without path juggling.
_RUNNER = textwrap.dedent('''\
    """Import a generated workflow and run it once against fakes."""

    from __future__ import annotations

    import asyncio
    import json
    import sys
    import traceback


    def report(**payload):
        print("__LOOM_SMOKE__" + json.dumps(payload))
        sys.exit(0 if payload.get("ok") else 1)


    def install_requested_fakes() -> None:
        """Swap toolset operations for stand-ins before the code imports them."""
        import importlib

        try:
            requested = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
        except json.JSONDecodeError:
            return

        from loom.agents.fakes import (
            executable_fake_toolset,
            install_fakes,
        )
        from loom.toolsets.registry import register_toolset

        for entry in requested:
            try:
                module_path, attribute = entry[1].rsplit(".", 1)
                manifest = getattr(importlib.import_module(module_path), attribute)
                install_fakes(manifest)
                # Register it executable too. A direct call inside a @step
                # binds to the module attribute the line above replaced;
                # ctx.agent(toolsets=[...]) asks the registry instead, and
                # without this it finds nothing — failing the very shape the
                # resolution ladder tells the model to emit for an ambiguity.
                faked = executable_fake_toolset(manifest)
                if faked is not None:
                    register_toolset(faked)
            except Exception:
                # A toolset that cannot be faked simply is not faked; the run
                # then fails the way it would have without this.
                continue


    def main() -> None:
        raw_input_arg = sys.argv[1] if len(sys.argv) > 1 else "null"
        try:
            workflow_input = json.loads(raw_input_arg)
        except json.JSONDecodeError:
            workflow_input = raw_input_arg

        install_requested_fakes()

        try:
            import generated_flow
        except Exception as exc:
            report(
                ok=False,
                phase="import",
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
            return

        from loom.runtime.workflow import WorkflowDefinition
        from loom.stores.memory import MemoryStore
        from loom import Runtime

        flows = [
            value
            for value in vars(generated_flow).values()
            if isinstance(value, WorkflowDefinition)
        ]
        names = [flow.name for flow in flows]
        if not flows:
            report(
                ok=False,
                phase="import",
                error="no @workflow-decorated function was found in the file",
                workflows_found=[],
            )
            return

        # A scripted model, so an agent call resolves without network or a key.
        try:
            from loom.agents.backend import BuiltInBackend
            from loom.testing import MockModelProvider, mock_response

            backend = BuiltInBackend(
                MockModelProvider(responses=[mock_response("mock agent reply")])
            )
        except Exception:
            backend = None

        # Time is faked, like the model provider. A workflow that waits four
        # minutes should cost a smoke run nothing, and the steps after the wait
        # are the ones worth exercising. Without this the run parks before
        # executing anything: reporting that as a pass certifies code that was
        # never entered, and reporting it as a failure pressures the repair loop
        # into deleting the wait — usually the one thing the spec asked for.
        real_sleep = asyncio.sleep

        async def _instant(_delay, *args, **kwargs):
            return await real_sleep(0, *args, **kwargs)

        asyncio.sleep = _instant
        runtime = Runtime(
            store=MemoryStore(),
            agent_backend=backend,
            inline_timer_threshold=10**9,
        )

        # A workflow annotated `text: str` handed None crashes on the first
        # attribute access, and the repair loop then tries to "fix" code that
        # was correct — the environmental-failure trap, in a new costume. Derive
        # an input from the workflow's own declared type instead.
        synthetic = False
        if workflow_input is None:
            try:
                from loom.agents.fakes import fake_value

                schema = flows[0].input_schema()
                if schema:
                    workflow_input = fake_value(schema)
                    synthetic = True
            except Exception:
                # A shape we cannot build is not a reason to fail the run;
                # None was what we had before.
                pass

        try:
            result = asyncio.run(runtime.run(flows[0], workflow_input))
        except Exception as exc:
            report(
                ok=False,
                phase="run",
                synthetic_input=synthetic,
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
                workflows_found=names,
            )
            return

        # A FAILED run is still a real failure of the generated code: report the
        # workflow's own error rather than claiming the smoke run passed.
        if result.status.value == "failed":
            report(
                ok=False,
                phase="run",
                synthetic_input=synthetic,
                error=result.error.message if result.error else "workflow failed",
                status=result.status.value,
                workflows_found=names,
            )
            return

        if result.status.value == "failed":
            report(
                ok=False,
                phase="run",
                synthetic_input=synthetic,
                error=result.error.message if result.error else "workflow failed",
                status=result.status.value,
                workflows_found=names,
            )
            return

        # Count only kinds that mean generated code actually ran. An allowlist,
        # because the alternatives are not all idle: ctx.sleep() journals a
        # clock read of its own, so "not a sleep entry" would count waiting as
        # working.
        executed = 0
        try:
            work = {"step", "agent", "model_call", "tool_call", "child_workflow"}
            entries = asyncio.run(runtime.store.load_journal(result.run_id))
            executed = sum(1 for e in entries if str(e.kind) in work)
        except Exception:
            executed = 0

        try:
            from loom.agents.outcome import empty_paths

            empties = empty_paths(result.output)
        except Exception:
            empties = []

        report(
            ok=True,
            phase="done",
            status=result.status.value,
            steps_executed=executed,
            output_preview=str(result.output)[:400],
            empty_paths=empties,
            workflows_found=names,
        )


    if __name__ == "__main__":
        main()
''')
