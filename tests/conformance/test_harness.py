"""The harness checked against itself.

A conformance suite is only worth its runtime if it fails when a store diverges.
This drives deliberately-broken stores through the same suite and asserts each
one is caught — the mutation-verification discipline applied to the thing doing
the verifying.

Each mutant below is modelled on a defect the matrix found on its first run
against real servers, so these are regression guards as much as self-checks.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from conformance.backends import ALL_BACKENDS, Backend, open_store
from loom.core.models import ExecutionRecord, TriggerRecord
from loom.stores.memory import MemoryStore


class TestTheMatrixIsComplete:
    def test_every_shipped_store_is_in_the_matrix(self) -> None:
        """A store added without a matrix row is a store nobody checks.

        The failure this prevents is silent: a fifth backend lands, its own
        tests pass, and it never meets the shared contract.
        """
        from pathlib import Path as _Path

        import loom.stores as stores

        exempt = {
            # Cache and locks only, deliberately: a journal wants durability and
            # queryability Redis is the wrong shape for, and a test asserts the
            # absence because the absence is the design.
            "redis",
        }
        # The modules, not the exported names: `loom.stores` also re-exports the
        # protocols, and asserting over those made this fail on `CacheStore`.
        shipped = {
            path.stem
            for path in _Path(stores.__file__).parent.glob("*.py")
            # Infrastructure shared by the backends rather than a backend:
            # `migrations` is the numbered DDL the SQL stores apply on connect,
            # exercised through them and through test_schema_migrations.py.
            if path.stem not in {"__init__", "base", "factory", "migrations"}
        }
        missing = shipped - {b.name for b in ALL_BACKENDS} - exempt
        assert not missing, f"stores with no conformance row: {sorted(missing)}"

    def test_an_unreachable_backend_is_named_not_dropped(self) -> None:
        """"Skipped" and "passed" must not look the same.

        A suite that quietly shrinks when a service is down reports green for
        coverage it did not have.
        """
        absent = Backend(
            "nowhere", env="LOOM_TEST_NOWHERE", default_url="postgresql://127.0.0.1:1/x"
        )
        reason = absent.why_not()
        assert reason, "an unreachable backend must give a reason"
        assert "no server" in reason and "LOOM_TEST_NOWHERE" in reason

    def test_a_missing_driver_reads_differently_from_a_down_server(self) -> None:
        no_driver = Backend("x", driver="not_a_real_driver_xyz")
        assert "not installed" in no_driver.why_not()


# ---------------------------------------------------------------------------
# The mutants
# ---------------------------------------------------------------------------


class _UnorderedJournal(MemoryStore):
    """Returns journal entries in the wrong order.

    The bug class: a store whose scan happens to be insertion-ordered in one
    engine and hash-ordered in another. Replay walks the journal by path, so
    disorder is silent until a run replays differently.
    """

    async def load_journal(self, run_id: str) -> list[Any]:
        return list(reversed(await super().load_journal(run_id)))


class _NonUniqueIdempotency(MemoryStore):
    """Ignores the idempotency key — the Mongo defect, in miniature.

    Mongo's unique index used ``sparse=True``, which skips documents *lacking*
    the field but not documents carrying an explicit ``null``. Every keyless
    run wrote ``idempotency_key: null``, so the second one collided and
    MongoStore could hold exactly one run.
    """

    async def find_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        return None


class _ImmediateExpiryCache(MemoryStore):
    """Treats ttl<=0 as "expires now" — the Mongo *and* Postgres defect.

    Both read ``ttl_seconds`` unconditionally, so ``set(key, value, 0)`` was a
    silent no-op on two of four stores while meaning "never expires" on the
    other two.
    """

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        await super().set(key, value, ttl_seconds)


class _LimitIgnoringList(MemoryStore):
    async def list_executions(self, **kwargs: Any) -> list[ExecutionRecord]:
        kwargs.pop("limit", None)
        return await super().list_executions(**kwargs)


class _FireThatDoesNotAdvance(MemoryStore):
    """``update_after_fire`` that does nothing — the Postgres defect.

    There it was worse than a no-op: the statement reused one parameter as both
    a timestamptz and a ``::text``, so asyncpg refused to prepare it and every
    cron trigger on Postgres raised instead of advancing.
    """

    async def update_after_fire(self, *args: Any, **kwargs: Any) -> None:
        return None


MUTANTS = [
    pytest.param(_UnorderedJournal, "journal order", id="journal-order"),
    pytest.param(_NonUniqueIdempotency, "idempotency", id="idempotency"),
    pytest.param(_ImmediateExpiryCache, "cache ttl", id="cache-ttl"),
    pytest.param(_LimitIgnoringList, "list limit", id="list-limit"),
    pytest.param(_FireThatDoesNotAdvance, "trigger fire", id="trigger-fire"),
]


@pytest.mark.parametrize(("mutant", "what"), MUTANTS)
async def test_the_suite_catches_a_divergent_store(
    mutant: type[MemoryStore], what: str
) -> None:
    """Run the real conformance assertions against a broken store.

    The suite's test bodies are reused directly rather than restated, so this
    cannot drift from what actually runs: if somebody deletes the ordering
    assertion, this stops catching ``_UnorderedJournal`` and fails.
    """
    import test_store_conformance as suite

    # The baseline first. Without it this test passes whenever a mutant fails
    # for *any* reason — including a harness bug of mine — and "the suite has
    # teeth" would be indistinguishable from "the suite is broken".
    clean = await _drive(suite, MemoryStore)
    assert not clean, f"the unmutated store already fails: {clean}"

    caught = await _drive(suite, mutant)
    assert caught, (
        f"a store that breaks {what} passed the entire conformance suite — "
        "the suite does not check it"
    )


async def _drive(suite: Any, factory: type[MemoryStore]) -> list[str]:
    """Every suite assertion that takes a store, each against a *fresh* one.

    A factory rather than an instance: the suite's own fixture builds a new
    store per test, and reusing one here leaked state between them — four
    assertions failed on the unmutated store, which the baseline above caught.
    """
    failures = []
    for holder in (
        suite.TestExecutions,
        suite.TestJournals,
        suite.TestEventsAndTimers,
        suite.TestCacheAndLocks,
        suite.TestTriggerStoreConformance,
    ):
        instance = holder()
        for name, method in inspect.getmembers(instance, inspect.ismethod):
            if not name.startswith("test_"):
                continue
            if "store" not in inspect.signature(method).parameters:
                continue
            try:
                await _call(method, factory())
            except Exception:  # any failure is a catch
                failures.append(f"{holder.__name__}.{name}")
    return failures


async def _call(method: Any, store: Any) -> None:
    parameters = inspect.signature(method).parameters
    kwargs: dict[str, Any] = {"store": store}
    # A couple of suite tests are themselves parametrized; give them a value.
    for name in parameters:
        if name in {"store", "self"}:
            continue
        kwargs[name] = 0
    outcome = method(**kwargs)
    if inspect.isawaitable(outcome):
        await outcome


class TestIsolationBetweenCases:
    """Each case gets a clean store, or the suite is order-dependent.

    An order-dependent conformance suite goes green for the wrong reason, which
    is worse than not having one.
    """

    @pytest.mark.parametrize("name", [b.name for b in ALL_BACKENDS])
    async def test_a_fresh_store_has_no_runs(self, name: str) -> None:
        async with open_store(name) as store:
            assert await store.list_executions() == []

    @pytest.mark.parametrize("name", [b.name for b in ALL_BACKENDS])
    async def test_writes_do_not_leak_into_the_next_case(self, name: str) -> None:
        async with open_store(name) as store:
            await store.create_execution(
                ExecutionRecord(run_id="leak-probe", workflow="w")
            )
            await store.save_trigger(
                TriggerRecord(trigger_id="leak-trg", workflow="w", kind="schedule")
            )
            assert len(await store.list_executions()) == 1

        async with open_store(name) as store:
            assert await store.get_execution("leak-probe") is None
            assert await store.get_trigger("leak-trg") is None
