"""Tests for DurabilityBackend protocol and EmbeddedBackend."""

from __future__ import annotations

import pytest

from workflow_builder.core.exceptions import BackendCapabilityError
from workflow_builder.runtime.backend import (
    Capabilities,
    Capability,
    DurabilityBackend,
    EmbeddedBackend,
)
from workflow_builder.runtime.engine import Runtime
from workflow_builder.state.memory import MemoryStore


class TestCapabilities:
    def test_has_capability(self) -> None:
        caps = Capabilities(supported=frozenset({Capability.JOURNAL, Capability.TIMERS}))
        assert caps.has(Capability.JOURNAL)
        assert caps.has(Capability.TIMERS)
        assert not caps.has(Capability.CONTINUE_AS_NEW)

    def test_require_present(self) -> None:
        caps = Capabilities(supported=frozenset({Capability.JOURNAL}))
        caps.require(Capability.JOURNAL, "test")  # should not raise

    def test_require_missing(self) -> None:
        caps = Capabilities(supported=frozenset())
        with pytest.raises(BackendCapabilityError, match="continue_as_new"):
            caps.require(Capability.CONTINUE_AS_NEW, "test")


class TestEmbeddedBackend:
    def test_name(self) -> None:
        eb = EmbeddedBackend(MemoryStore())
        assert eb.name == "embedded"

    def test_capabilities(self) -> None:
        eb = EmbeddedBackend(MemoryStore())
        caps = eb.capabilities()
        assert caps.has(Capability.JOURNAL)
        assert caps.has(Capability.TIMERS)
        assert caps.has(Capability.EVENTS)
        assert caps.has(Capability.CHILD_WORKFLOWS)
        assert caps.has(Capability.SIGNALS)
        assert not caps.has(Capability.CONTINUE_AS_NEW)
        assert not caps.has(Capability.KV)

    def test_satisfies_protocol(self) -> None:
        eb = EmbeddedBackend(MemoryStore())
        assert isinstance(eb, DurabilityBackend)

    def test_invalid_store_type(self) -> None:
        with pytest.raises(TypeError, match="ExecutionStore"):
            EmbeddedBackend("not a store")


class TestRuntimeBackendIntegration:
    def test_default_creates_embedded(self) -> None:
        rt = Runtime()
        assert isinstance(rt.backend, EmbeddedBackend)
        assert rt.backend.name == "embedded"

    def test_store_param_wraps_in_embedded(self) -> None:
        store = MemoryStore()
        rt = Runtime(store=store)
        assert isinstance(rt.backend, EmbeddedBackend)
        assert rt.store is store

    def test_explicit_backend(self) -> None:
        eb = EmbeddedBackend(MemoryStore())
        rt = Runtime(backend=eb)
        assert rt.backend is eb

    @pytest.mark.asyncio
    async def test_end_to_end_with_backend(self) -> None:
        from workflow_builder import Context, step, workflow

        @step
        async def double(x: int) -> int:
            return x * 2

        @workflow
        async def math_wf(ctx: Context, x: int) -> int:
            return await ctx.step(double, x)

        rt = Runtime()
        result = await rt.run(math_wf, 5)
        assert result.output == 10
        assert rt.backend.name == "embedded"
