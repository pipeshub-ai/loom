"""Tests for StepClass enum and @pure/@effect/@node decorators."""

from __future__ import annotations

import pytest

from workflow_builder.core.exceptions import ConfigurationError
from workflow_builder.core.retry import DEFAULT_RETRY, NO_RETRY
from workflow_builder.steps.definition import (
    StepClass,
    StepDefinition,
    effect,
    node,
    pure,
    step,
)

# ---------------------------------------------------------------------------
# Bare decorator usage (no arguments)
# ---------------------------------------------------------------------------


class TestBareDecorators:
    def test_step_bare(self) -> None:
        @step
        async def fetch_data(url: str) -> dict:
            return {}

        assert isinstance(fetch_data, StepDefinition)
        assert fetch_data.klass is StepClass.EFFECT
        assert fetch_data.name == "fetch_data"

    def test_pure_bare(self) -> None:
        @pure
        async def format_name(first: str, last: str) -> str:
            return f"{first} {last}"

        assert isinstance(format_name, StepDefinition)
        assert format_name.klass is StepClass.PURE
        assert format_name.name == "format_name"
        assert format_name.is_pure is True
        assert format_name.is_journaled is False
        assert format_name.retry == NO_RETRY

    def test_effect_bare(self) -> None:
        @effect
        async def send_email(to: str) -> bool:
            return True

        assert isinstance(send_email, StepDefinition)
        assert send_email.klass is StepClass.EFFECT
        assert send_email.is_pure is False
        assert send_email.is_journaled is True

    def test_node_bare(self) -> None:
        @node
        async def process(data: dict) -> dict:
            return data

        assert isinstance(process, StepDefinition)
        assert process.klass is StepClass.NODE
        assert process.is_journaled is True


# ---------------------------------------------------------------------------
# Decorator with arguments
# ---------------------------------------------------------------------------


class TestDecoratorWithArgs:
    def test_step_with_name(self) -> None:
        @step(name="custom_fetch")
        async def fetch(url: str) -> dict:
            return {}

        assert fetch.name == "custom_fetch"

    def test_pure_with_name(self) -> None:
        @pure(name="my_transform")
        async def transform(data: dict) -> dict:
            return data

        assert transform.name == "my_transform"
        assert transform.klass is StepClass.PURE

    def test_effect_with_retry(self) -> None:
        @effect(retry=5, timeout=30)
        async def api_call(url: str) -> dict:
            return {}

        assert api_call.retry.max_attempts == 5
        assert api_call.timeout == 30

    def test_effect_with_idempotency(self) -> None:
        def idem_key(order_id: str) -> str:
            return f"charge:{order_id}"

        @effect(idempotency=idem_key)
        async def charge(order_id: str) -> dict:
            return {}

        assert charge.idempotency is idem_key

    def test_node_with_timeout(self) -> None:
        @node(timeout=60)
        async def slow_node(data: dict) -> dict:
            return data

        assert slow_node.timeout == 60
        assert slow_node.klass is StepClass.NODE

    def test_pure_no_retry_override(self) -> None:
        """Pure steps always have NO_RETRY regardless."""
        @pure
        async def compute(x: int) -> int:
            return x * 2

        assert compute.retry == NO_RETRY

    def test_step_default_retry(self) -> None:
        @step
        async def fetch(url: str) -> str:
            return ""

        assert fetch.retry == DEFAULT_RETRY


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestStepHashes:
    def test_contract_hash_exists(self) -> None:
        @effect
        async def typed_step(x: int, y: str) -> float:
            return 0.0

        assert typed_step.contract_hash != ""

    def test_closure_hash_exists(self) -> None:
        @effect
        async def some_step(x: int) -> int:
            return x + 1

        assert some_step.closure_hash != ""

    def test_contract_hash_stable(self) -> None:
        @effect
        async def step_a(x: int) -> str:
            return str(x)

        @effect
        async def step_b(x: int) -> str:
            return str(x * 2)

        # Same signature → same contract hash
        assert step_a.contract_hash == step_b.contract_hash

    def test_contract_hash_changes_on_type_change(self) -> None:
        @effect
        async def step_int(x: int) -> int:
            return x

        @effect
        async def step_str(x: str) -> str:
            return x

        assert step_int.contract_hash != step_str.contract_hash

    def test_closure_hash_changes_on_body_change(self) -> None:
        @effect
        async def body_a(x: int) -> int:
            return x + 1

        @effect
        async def body_b(x: int) -> int:
            return x + 2

        assert body_a.closure_hash != body_b.closure_hash


# ---------------------------------------------------------------------------
# with_options
# ---------------------------------------------------------------------------


class TestWithOptions:
    def test_preserves_klass(self) -> None:
        @pure
        async def original(x: int) -> int:
            return x

        clone = original.with_options(name="clone")
        assert clone.klass is StepClass.PURE
        assert clone.name == "clone"

    def test_preserves_idempotency(self) -> None:
        def idem(x: str) -> str:
            return f"key:{x}"

        @effect(idempotency=idem)
        async def original(x: str) -> str:
            return x

        clone = original.with_options(timeout=10)
        assert clone.idempotency is idem


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestDecoratorErrors:
    def test_double_decoration_raises(self) -> None:
        @step
        async def my_step(x: int) -> int:
            return x

        with pytest.raises(ConfigurationError, match="already a step"):
            step(my_step)

    def test_double_pure_raises(self) -> None:
        @pure
        async def my_pure(x: int) -> int:
            return x

        with pytest.raises(ConfigurationError, match="already a step"):
            pure(my_pure)


# ---------------------------------------------------------------------------
# Repr
# ---------------------------------------------------------------------------


class TestRepr:
    def test_repr_shows_klass(self) -> None:
        @pure
        async def my_func(x: int) -> int:
            return x

        assert repr(my_func) == "<pure my_func>"

    def test_repr_effect(self) -> None:
        @effect
        async def api_call(url: str) -> dict:
            return {}

        assert repr(api_call) == "<effect api_call>"

    def test_repr_node(self) -> None:
        @node
        async def transform(data: dict) -> dict:
            return data

        assert repr(transform) == "<node transform>"


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------


class TestDirectInvocation:
    @pytest.mark.asyncio
    async def test_step_is_callable(self) -> None:
        @step
        async def double(x: int) -> int:
            return x * 2

        result = await double(5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_pure_is_callable(self) -> None:
        @pure
        async def upper(s: str) -> str:
            return s.upper()

        result = await upper("hello")
        assert result == "HELLO"
