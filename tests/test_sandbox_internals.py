"""Unit tests for the shared sandbox harness, conversation loop, and
Docker command construction — no daemon, no child process required.

The behavioural suite in ``test_sandbox.py`` and the isolation suite in
``test_docker_sandbox.py`` already cover the adapters end to end. These
tests pin the pieces those adapters share, so a change to the wire format
or the ``docker run`` argv is caught without paying for a container.
"""

from __future__ import annotations

import ast
import json

import pytest

from loom.runtime.effects import EffectCall, EffectResult
from loom.runtime.sandbox import SandboxPolicy
from loom.runtime.sandboxes._conversation import converse
from loom.runtime.sandboxes._harness import _SHIM_MARKER, build_child_script
from loom.runtime.sandboxes.docker import DockerSandbox
from loom.runtime.sandboxes.subprocess import SubprocessSandbox


class TestBuildChildScript:
    def test_the_default_script_is_valid_python(self) -> None:
        script = build_child_script()
        ast.parse(script)
        compile(script, "<child>", "exec")

    def test_the_default_script_does_not_contain_the_shim_marker(self) -> None:
        assert _SHIM_MARKER not in build_child_script()

    def test_a_shim_is_injected_into_the_ctx_class_body(self) -> None:
        shim = "    def extra(self):\n        return 7\n"
        script = build_child_script(ctx_shims=shim)
        ast.parse(script)
        compile(script, "<child>", "exec")
        assert "def extra(self):" in script
        assert _SHIM_MARKER not in script

    def test_a_shim_that_redefines_step_is_still_valid_python(self) -> None:
        shim = (
            "    async def step(self, target, *positional, name=None, **arguments):\n"
            "        return await self._call('step', target, arguments, 'write', name=name)\n"
        )
        script = build_child_script(ctx_shims=shim)
        compile(script, "<child>", "exec")
        assert script.count("async def step") == 2


class _Writer:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def readline(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self) -> bytes:
        remaining = b"".join(self._chunks)
        self._chunks.clear()
        return remaining


class _Process:
    def __init__(self, lines: list[bytes], stderr: bytes = b"") -> None:
        self.stdin = _Writer()
        self.stdout = _Reader(lines)
        self.stderr = _Reader([stderr] if stderr else [])


class _RecordingChannel:
    def __init__(self, reply: EffectResult | None = None) -> None:
        self.seen: list[EffectCall] = []
        self._reply = reply or EffectResult(value="replied")

    async def dispatch(self, call: EffectCall) -> EffectResult:
        self.seen.append(call)
        return self._reply


class TestConverse:
    async def test_a_done_message_returns_the_value(self) -> None:
        process = _Process([b'{"t":"done","value":42}\n'])
        outcome = await converse(process, "src", "flow", "run-1", {}, _RecordingChannel())

        assert outcome.ok
        assert outcome.value == 42
        bootstrap = json.loads(process.stdin.chunks[0])
        assert bootstrap["source"] == "src"
        assert bootstrap["entrypoint"] == "flow"
        assert bootstrap["run_id"] == "run-1"

    async def test_an_error_message_returns_a_failed_outcome(self) -> None:
        process = _Process([b'{"t":"error","error":"boom"}\n'])
        outcome = await converse(process, "src", "flow", "run-1", {}, _RecordingChannel())

        assert not outcome.ok
        assert outcome.error == "boom"

    async def test_a_call_is_dispatched_and_the_encoded_reply_written(self) -> None:
        process = _Process(
            [
                b'{"t":"call","kind":"step","target":"double","arguments":{"value":2},"effect":"write","name":"double"}\n',
                b'{"t":"done","value":4}\n',
            ]
        )
        channel = _RecordingChannel(EffectResult(value=4))
        outcome = await converse(process, "src", "flow", "run-1", {}, channel)

        assert outcome.ok
        assert outcome.value == 4
        assert outcome.calls == 1
        assert channel.seen[0].target == "double"
        assert channel.seen[0].arguments == {"value": 2}
        reply = json.loads(process.stdin.chunks[1])
        assert reply["ok"] is True
        assert reply["value"] == 4

    async def test_eof_without_a_terminal_message_is_a_failure(self) -> None:
        process = _Process([], stderr=b"killed by the kernel\n")
        outcome = await converse(process, "src", "flow", "run-1", {}, _RecordingChannel())

        assert not outcome.ok
        assert "killed by the kernel" in outcome.error

    async def test_eof_with_empty_stderr_names_the_silent_exit(self) -> None:
        process = _Process([])
        outcome = await converse(process, "src", "flow", "run-1", {}, _RecordingChannel())

        assert not outcome.ok
        assert "without reporting" in outcome.error


class TestDockerCommand:
    def test_network_is_disabled_and_the_root_is_read_only(self) -> None:
        sandbox = DockerSandbox("loom-sandbox:test")
        command = sandbox._docker_command("loom-sbx-test", SandboxPolicy())

        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert command[command.index("--user") + 1] == "sandbox"
        assert "--cap-drop" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert "--security-opt" in command
        assert "--pids-limit" in command

    def test_memory_limit_is_applied_when_the_policy_asks_for_one(self) -> None:
        sandbox = DockerSandbox("unused:latest")
        command = sandbox._docker_command(
            "loom-sbx-test", SandboxPolicy(max_memory_mb=256)
        )
        assert command[command.index("--memory") + 1] == "256m"
        assert "--memory-swap" in command

    def test_no_memory_flags_when_the_policy_asks_for_none(self) -> None:
        sandbox = DockerSandbox("unused:latest")
        command = sandbox._docker_command("loom-sbx-test", SandboxPolicy())
        assert "--memory" not in command

    def test_cpu_seconds_becomes_an_in_container_ulimit(self) -> None:
        sandbox = DockerSandbox("unused:latest")
        command = sandbox._docker_command(
            "loom-sbx-test", SandboxPolicy(max_cpu_seconds=30)
        )
        shell_command = command[command.index("sh") + 2]
        assert "ulimit -t" in shell_command
        assert "30" in command

    def test_no_ulimit_when_cpu_seconds_is_unset(self) -> None:
        sandbox = DockerSandbox("unused:latest")
        command = sandbox._docker_command("loom-sbx-test", SandboxPolicy())
        shell_command = command[command.index("sh") + 2]
        assert "ulimit" not in shell_command

    def test_the_child_script_is_its_own_argv_element(self) -> None:
        sandbox = DockerSandbox("unused:latest")
        command = sandbox._docker_command("loom-sbx-test", SandboxPolicy())
        child = sandbox._child
        assert child in command
        actual_shell_string = command[command.index("sh") + 2]
        assert child not in actual_shell_string

    def test_environment_is_an_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.setenv("SECRET", "must-not-leak")
        env = DockerSandbox._environment(SandboxPolicy(allowed_env=frozenset({"TZ"})))
        assert env == {"TZ": "UTC"}

    def test_enforces_is_a_superset_of_subprocess(self) -> None:
        docker = DockerSandbox("unused:latest")
        subprocess = SubprocessSandbox()
        assert subprocess.enforces <= docker.enforces
        assert "network" in docker.enforces - subprocess.enforces

    def test_container_name_is_safe_and_unique(self) -> None:
        sandbox = DockerSandbox("unused:latest")
        first = sandbox._container_name("run/with spaces!")
        second = sandbox._container_name("run/with spaces!")
        assert first.startswith("loom-sbx-")
        assert first != second
        assert " " not in first
        assert "/" not in first
