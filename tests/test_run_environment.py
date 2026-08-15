"""Per-run environment: precedence, bounds, redaction, reserved keys."""

from __future__ import annotations

import logging

import pytest

from loom.core.exceptions import ConfigurationError
from loom.facade import describe_record
from loom.runtime.environment import (
    MAX_ENV_BYTES,
    MAX_ENV_KEYS,
    RunEnvironment,
    validate_run_env,
)


class TestRunEnvironment:
    def test_precedence_is_run_then_runtime_then_os(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHARED", "os")
        monkeypatch.setenv("OS_ONLY", "from-os")
        env = RunEnvironment(
            run_env={"SHARED": "run", "RUN_ONLY": "r"},
            runtime_env={"SHARED": "runtime", "RUNTIME_ONLY": "rt"},
        )
        assert env["SHARED"] == "run"
        assert env["RUNTIME_ONLY"] == "rt"
        assert env["OS_ONLY"] == "from-os"
        assert env.get("missing") is None
        assert "RUN_ONLY" in env
        assert env.overrides() == {"SHARED": "run", "RUN_ONLY": "r"}

    def test_has_no_iter(self) -> None:
        env = RunEnvironment(run_env={"A": "1"})
        assert not hasattr(env, "__iter__")
        with pytest.raises(TypeError):
            dict(env)  # type: ignore[arg-type]

    def test_does_not_mutate_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FRESH_KEY", raising=False)
        env = RunEnvironment(run_env={"FRESH_KEY": "v"})
        assert env["FRESH_KEY"] == "v"
        import os

        assert "FRESH_KEY" not in os.environ


class TestValidateRunEnv:
    def test_secret_looking_keys_warn_once(self, caplog: pytest.LogCaptureFixture) -> None:
        from loom.runtime import environment as env_mod

        env_mod._warned_keys.clear()
        with caplog.at_level(logging.WARNING, logger="workflow.environment"):
            validate_run_env({"API_TOKEN": "nope", "REGION": "eu"})
            validate_run_env({"API_TOKEN": "nope"})
        warnings = [r for r in caplog.records if "API_TOKEN" in r.getMessage()]
        assert len(warnings) == 1
        assert "credentials=" in warnings[0].getMessage()

    def test_rejects_too_many_keys(self) -> None:
        with pytest.raises(ConfigurationError, match="limit"):
            validate_run_env({f"k{i}": "v" for i in range(MAX_ENV_KEYS + 1)})

    def test_rejects_oversized_payload(self) -> None:
        huge = "x" * (MAX_ENV_BYTES + 1)
        with pytest.raises(ConfigurationError, match="bytes"):
            validate_run_env({"k": huge})

    def test_rejects_non_strings(self) -> None:
        with pytest.raises(ConfigurationError, match="strings"):
            validate_run_env({"k": 1})  # type: ignore[dict-item]


class TestEnvOnARun:
    async def test_env_is_readable_from_the_workflow_body(self) -> None:
        from loom import Context, Runtime, workflow

        @workflow(name="echo_region")
        async def echo_region(ctx: Context, _: None = None) -> str:
            return ctx.env["REGION"]

        rt = Runtime()
        result = await rt.run(echo_region, env={"REGION": "eu-west"})
        assert result.output == "eu-west"
        record = await rt.get(result.run_id)
        assert record is not None
        assert record.metadata["loom.env"] == {"REGION": "eu-west"}

    async def test_env_is_restored_on_replay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loom import Context, Runtime, workflow

        @workflow(name="echo_region_replay")
        async def echo_region(ctx: Context, _: None = None) -> str:
            return ctx.env["REGION"]

        rt = Runtime()
        first = await rt.run(echo_region, env={"REGION": "original"})
        monkeypatch.setenv("REGION", "os-now")
        replayed = await rt.replay(first.run_id)
        assert replayed.output == "original"

    async def test_describe_record_redacts_loom_env(self) -> None:
        from loom import Context, Runtime, workflow

        @workflow(name="redact_env")
        async def redact_env(ctx: Context, _: None = None) -> str:
            return "ok"

        rt = Runtime()
        result = await rt.run(redact_env, env={"REGION": "s3cret-value"})
        record = await rt.get(result.run_id)
        assert record is not None
        view = describe_record(record)
        assert "loom.env" not in view["metadata"]
        assert "s3cret-value" not in str(view)

    async def test_reserved_metadata_keys_are_stripped(self) -> None:
        from loom import Context, Runtime, workflow

        @workflow(name="strip_reserved")
        async def strip_reserved(ctx: Context, _: None = None) -> str:
            return ctx.env.get("REAL", "missing")

        rt = Runtime()
        result = await rt.run(
            strip_reserved,
            env={"REAL": "from-env"},
            metadata={"loom.env": {"REAL": "injected"}, "caller": "test"},
        )
        record = await rt.get(result.run_id)
        assert record is not None
        assert record.metadata["loom.env"] == {"REAL": "from-env"}
        assert record.metadata["caller"] == "test"
        assert result.output == "from-env"


class TestStartRunRequestStripsReserved:
    def test_http_body_drops_loom_env(self) -> None:
        from loom.server.app import StartRunRequest

        body = StartRunRequest(
            workflow="x",
            metadata={"loom.env": {"HACK": "1"}, "ok": "yes"},
        )
        assert "loom.env" not in body.metadata
        assert body.metadata["ok"] == "yes"
