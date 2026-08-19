"""Credentials must not survive in what a journal shows.

The defect these pin: a step's recorded input is written for humans and never
replayed, and nothing on that path redacted anything, so

    await ctx.step(call_api, "hello", api_key)

durably recorded ``{'args': ['hello', 'sk-SUPER-SECRET-123'], 'kwargs': {}}``
and ``loom show`` printed it back.

Every test here asserts the *absence* of a literal secret in the stored bytes
rather than the presence of ``***``. Checking for the marker passes if the
redaction ran somewhere and the secret also survived somewhere else.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, SecretStr

from loom import Context, Runtime, step, workflow
from loom.core.redaction import (
    DEFAULT_REDACT_KEYS,
    REDACTED,
    is_secret_name,
    redact,
    redact_call_input,
)
from loom.core.secret import Secret
from loom.core.serde import decode, encode
from loom.facade import LocalFacade
from loom.stores.memory import MemoryStore

SECRET = "sk-SUPER-SECRET-123"


async def journal_text(runtime: Runtime, run_id: str) -> str:
    """Everything the journal holds for a run, as one searchable string."""
    entries = await runtime.store.load_journal(run_id)
    return str([(e.name, e.input, e.output) for e in entries])


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


class TestSecretNames:
    @pytest.mark.parametrize(
        "name",
        [
            "api_key",
            "openai_api_key",
            "qb_api_key",
            "apiKey",
            "X-Api-Key",
            "token",
            "access_token",
            "refresh_token",
            "password",
            "db_password",
            "authorization",
            "AUTHORIZATION",
            "client_secret",
            "secret",
            "secret_key",
            "signing_secret",
            "bearer",
            "credentials",
        ],
    )
    def test_credential_names_match(self, name: str) -> None:
        assert is_secret_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            # Each of these is a real name that a substring test would eat.
            "keyboard",
            "tokenizer_config",
            "secret_santa_list",
            "token_endpoint",
            "cache_key",
            "idempotency_key",
            "partition_key",
            "key",
            "author",
            "authority",
            "name",
            "email",
            "description",
        ],
    )
    def test_ordinary_names_do_not_match(self, name: str) -> None:
        """A denylist that redacts ordinary data is one people switch off."""
        assert not is_secret_name(name)

    def test_an_empty_key_set_redacts_nothing(self) -> None:
        assert redact({"api_key": SECRET}, frozenset()) == {"api_key": SECRET}


# ---------------------------------------------------------------------------
# Building a step's recorded input
# ---------------------------------------------------------------------------


class TestRecordedInput:
    def test_a_positional_credential_is_bound_to_its_parameter(self) -> None:
        """The case a key denylist cannot see, and the one everybody writes."""

        def parse(text: str, api_key: str) -> str: ...

        recorded = redact_call_input(parse, ("hello", SECRET), {})
        assert recorded == {"args": ["hello", REDACTED], "kwargs": {}}

    def test_a_keyword_credential_is_matched_by_name(self) -> None:
        def parse(text: str, api_key: str) -> str: ...

        recorded = redact_call_input(parse, ("hello",), {"api_key": SECRET})
        assert recorded == {"args": ["hello"], "kwargs": {"api_key": REDACTED}}

    def test_a_position_past_star_args_is_left_alone(self) -> None:
        """No name to report is not the same as a name that looks safe.

        Guessing here would mean redacting by position, which has no meaning.
        """

        def collect(*items: str) -> None: ...

        assert redact_call_input(collect, ("a", "b"), {}) == {
            "args": ["a", "b"],
            "kwargs": {},
        }

    def test_an_unreadable_signature_degrades_rather_than_raising(self) -> None:
        assert redact_call_input(None, ("a",), {"token": SECRET}) == {
            "args": ["a"],
            "kwargs": {"token": REDACTED},
        }

    def test_ordinary_arguments_survive(self) -> None:
        """Redaction must not cost the debugging aid it is protecting."""

        def parse(text: str, api_key: str, retries: int) -> None: ...

        recorded = redact_call_input(parse, ("hello", SECRET, 3), {})
        assert recorded["args"] == ["hello", REDACTED, 3]


# ---------------------------------------------------------------------------
# End to end, through a real run
# ---------------------------------------------------------------------------


class Config(BaseModel):
    user: str
    openai_api_key: str = ""


@step
async def call_api(text: str, api_key: str) -> str:
    return "ok"


@step
async def with_config(config: Config) -> str:
    return "ok"


@step
async def with_opaque(text: str, blob: object) -> str:
    return "ok"


class TestJournalDoesNotHoldSecrets:
    @pytest.mark.asyncio()
    async def test_a_positional_secret_never_reaches_the_journal(self) -> None:
        @workflow(name="redact_positional")
        async def flow(ctx: Context, _: None = None) -> str:
            return await ctx.step(call_api, "hello", SECRET)

        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(flow)
        assert SECRET not in await journal_text(runtime, result.run_id)

    @pytest.mark.asyncio()
    async def test_a_model_field_never_reaches_the_journal(self) -> None:
        """Encoding turns the model into a mapping, so one rule covers both."""

        @workflow(name="redact_model")
        async def flow(ctx: Context, _: None = None) -> str:
            return await ctx.step(
                with_config, Config(user="ada", openai_api_key=SECRET)
            )

        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(flow)
        assert SECRET not in await journal_text(runtime, result.run_id)

    @pytest.mark.asyncio()
    async def test_a_secret_no_longer_erases_its_siblings(self) -> None:
        """``Secret`` refuses to encode, and used to take the whole input down.

        The recorded input became ``{'__unserializable__': 'dict'}`` — safe,
        and telling a reader nothing about the arguments beside it.
        """

        @workflow(name="redact_secret_type")
        async def flow(ctx: Context, _: None = None) -> str:
            return await ctx.step(with_opaque, "keep-me", Secret(SECRET))

        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(flow)
        entries = await runtime.store.load_journal(result.run_id)
        recorded = next(e.input for e in entries if e.name == "with_opaque")

        assert recorded == {"args": ["keep-me", REDACTED], "kwargs": {}}
        assert "__unserializable__" not in str(recorded)

    @pytest.mark.asyncio()
    async def test_an_empty_key_set_restores_the_old_behaviour(self) -> None:
        """The escape hatch, for a codebase that wants inputs verbatim."""

        @workflow(name="redact_disabled")
        async def flow(ctx: Context, _: None = None) -> str:
            return await ctx.step(call_api, "hello", SECRET)

        runtime = Runtime(store=MemoryStore(), redact_keys=frozenset())
        result = await runtime.run(flow)
        assert SECRET in await journal_text(runtime, result.run_id)

    @pytest.mark.asyncio()
    async def test_a_house_name_can_be_added(self) -> None:
        @step
        async def house(text: str, wombat: str) -> str:
            return "ok"

        @workflow(name="redact_custom")
        async def flow(ctx: Context, _: None = None) -> str:
            return await ctx.step(house, "hello", SECRET)

        runtime = Runtime(
            store=MemoryStore(), redact_keys={*DEFAULT_REDACT_KEYS, "wombat"}
        )
        result = await runtime.run(flow)
        assert SECRET not in await journal_text(runtime, result.run_id)


class TestPydanticSecretStr:
    """Pinning behaviour LOOM did not add and must not lose.

    ``SecretStr`` has always encoded as ``'**********'``. Reimplementing that
    would be duplication; not testing it would let a serialiser change take it
    away silently.
    """

    def test_a_secret_str_encodes_redacted(self) -> None:
        assert encode(SecretStr(SECRET)) == "**********"

    def test_a_secret_str_field_encodes_redacted(self) -> None:
        class Held(BaseModel):
            user: str
            api_key: SecretStr

        assert encode(Held(user="ada", api_key=SecretStr(SECRET))) == {
            "user": "ada",
            "api_key": "**********",
        }


# ---------------------------------------------------------------------------
# The display boundary
# ---------------------------------------------------------------------------


class TestRunInputAtRestAndOnTheWire:
    """A run's input is *replayed*, so it cannot be redacted at rest.

    Which makes this the one place the two halves have to differ: intact in the
    store, absent from anything a person or an HTTP client is shown.
    """

    @pytest.mark.asyncio()
    async def test_the_wire_shape_hides_it_and_the_record_keeps_it(self) -> None:
        @workflow(name="redact_facade")
        async def flow(ctx: Context, config: Config) -> dict[str, str]:
            return {"user": config.user, "token": "sk-IN-THE-OUTPUT"}

        runtime = Runtime(store=MemoryStore())
        runtime.register(flow)
        result = await runtime.run(flow, Config(user="ada", openai_api_key=SECRET))

        shown = await LocalFacade(runtime=runtime).get(result.run_id)
        assert shown is not None
        assert shown["input"] == {"user": "ada", "openai_api_key": REDACTED}
        assert shown["output"] == {"user": "ada", "token": REDACTED}

        record = await runtime.get(result.run_id)
        assert record is not None
        assert decode(record.input)["openai_api_key"] == SECRET, (
            "the body receives this on every re-entry — redacting at rest "
            "would change what the workflow runs with"
        )


# ---------------------------------------------------------------------------
# What is deliberately not covered
# ---------------------------------------------------------------------------


class TestTheResidual:
    @pytest.mark.asyncio()
    async def test_a_secret_inside_a_string_is_not_caught(self) -> None:
        """Asserted so nobody mistakes this for complete coverage.

        Catching it means guessing at the *contents* of a value, and a
        heuristic that redacts anything resembling a key eventually eats a
        legitimate identifier. This is why the reference-workflow rules forbid
        passing a credential as a step argument at all.
        """

        @step
        async def send(header: str) -> str:
            return "ok"

        @workflow(name="redact_residual")
        async def flow(ctx: Context, _: None = None) -> str:
            return await ctx.step(send, f"Bearer {SECRET}")

        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(flow)
        assert SECRET in await journal_text(runtime, result.run_id)
