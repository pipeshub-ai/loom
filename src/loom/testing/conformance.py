"""Prove your own ``EventLog`` and ``Checkpoints`` are correct.

LOOM ships no broker. It ships the protocols, one reference implementation, and
this — so a host writing a Kafka, Redis Streams, NATS, or Postgres adapter can
assert the contract in one call:

    from loom.testing.conformance import verify_event_log

    async def test_my_redis_streams_log():
        await verify_event_log(lambda: MyRedisStreamsLog(url))

**This is the deliverable, not the adapters.** A protocol without an executable
contract is a suggestion, and substitutability is exactly the property that holds
for the in-memory implementation and quietly stops holding for the distributed
one. It also runs the other way: LOOM cannot tighten a contract later without
these failing loudly in every downstream repo, which is the point.

What is asserted here is deliberately the *whole* promise and no more. In
particular it does **not** assert a total order across keys, because the port
does not promise one — an adapter held to a stronger contract than the protocol
states is an adapter that cannot be written for a partitioned backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from loom.events.models import EventRecord, RetentionPolicy

__all__ = ["verify_checkpoints", "verify_event_log", "verify_event_source"]

Factory = Callable[[], Any]


def _record(event_id: str, *, key: str = "", type: str = "test.event") -> EventRecord:
    return EventRecord(
        event_id=event_id, type=type, payload={"id": event_id}, key=key
    )


async def _maybe(value: Any) -> Any:
    """Await *value* if it is awaitable. Lets a factory be sync or async."""
    if isinstance(value, Awaitable):
        return await value
    return value


async def verify_event_log(factory: Factory, *, topic: str = "conformance") -> None:
    """Assert that *factory* builds a conforming :class:`~loom.events.EventLog`.

    *factory* is called more than once and **must return a handle to the same
    underlying storage each time** — that is what proves resume across a restart
    rather than merely across a variable.
    """
    await _round_trip(factory, topic)
    await _reads_never_repeat_or_skip(factory, f"{topic}-cursor")
    await _append_is_idempotent(factory, f"{topic}-idem")
    await _same_key_keeps_order(factory, f"{topic}-order")
    await _empty_topic_is_none_not_error(factory, f"{topic}-empty")
    await _limit_is_respected(factory, f"{topic}-limit")
    await _survives_a_restart(factory, f"{topic}-restart")
    await _concurrent_appends_lose_nothing(factory, f"{topic}-concurrent")
    await _a_failed_append_is_invisible(factory, f"{topic}-crash")
    await _retention_never_resurrects(factory, f"{topic}-retain")
    await _wait_for_returns(factory, f"{topic}-wait")


async def _round_trip(factory: Factory, topic: str) -> None:
    log = await _maybe(factory())
    positions = await log.append(topic, [_record("a"), _record("b")])

    assert len(positions) == 2, "append must return one position per record"
    found = await log.read(topic, after=None, limit=10)
    assert [e.event_id for e in found] == ["a", "b"], (
        "read(after=None) must return everything retained, in append order"
    )
    assert [e.position for e in found] == list(positions), (
        "the positions read back must be the positions append returned"
    )


async def _reads_never_repeat_or_skip(factory: Factory, topic: str) -> None:
    """The core of resume, and the only promise `Position` carries."""
    log = await _maybe(factory())
    await log.append(topic, [_record(f"e{i}") for i in range(10)])

    seen: list[str] = []
    after = None
    while True:
        batch = await log.read(topic, after=after, limit=3)
        if not batch:
            break
        seen.extend(e.event_id for e in batch)
        after = batch[-1].position

    assert seen == [f"e{i}" for i in range(10)], (
        f"paging with after= must see every record exactly once; saw {seen}"
    )
    assert len(seen) == len(set(seen)), "a record was returned twice"


async def _append_is_idempotent(factory: Factory, topic: str) -> None:
    """What makes a producer safe to retry after a crash."""
    log = await _maybe(factory())
    first = await log.append(topic, [_record("dup")])
    second = await log.append(topic, [_record("dup")])

    assert first == second, (
        "re-appending an event_id must return its original position, not a new one"
    )
    found = await log.read(topic, after=None, limit=10)
    assert len(found) == 1, (
        f"re-appending an event_id must store nothing new; found {len(found)} records"
    )


async def _same_key_keeps_order(factory: Factory, topic: str) -> None:
    """Per-key ordering is the whole ordering promise.

    Interleaved with another key on purpose: an adapter that only preserves
    order when a topic has a single key passes a naive test and loses ordering
    the moment two entities are active at once.
    """
    log = await _maybe(factory())
    await log.append(topic, [
        _record("x1", key="X"), _record("y1", key="Y"),
        _record("x2", key="X"), _record("y2", key="Y"),
        _record("x3", key="X"),
    ])

    found = await log.read(topic, after=None, limit=50)
    for key, expected in (("X", ["x1", "x2", "x3"]), ("Y", ["y1", "y2"])):
        got = [e.event_id for e in found if e.record.key == key]
        assert got == expected, f"records keyed {key} came back as {got}"
    # Deliberately no assertion about the order *between* X and Y.


async def _empty_topic_is_none_not_error(factory: Factory, topic: str) -> None:
    log = await _maybe(factory())
    assert await log.head(topic) is None, "head() of an empty topic must be None"
    assert list(await log.read(topic, after=None, limit=10)) == [], (
        "reading an empty topic must return nothing, not raise"
    )


async def _limit_is_respected(factory: Factory, topic: str) -> None:
    log = await _maybe(factory())
    await log.append(topic, [_record(f"e{i}") for i in range(5)])

    assert len(await log.read(topic, after=None, limit=2)) == 2
    assert list(await log.read(topic, after=None, limit=0)) == [], (
        "limit=0 must return nothing rather than everything"
    )


async def _survives_a_restart(factory: Factory, topic: str) -> None:
    """The property the whole design exists for: kill it, start it, resume."""
    writer = await _maybe(factory())
    await writer.append(topic, [_record("before"), _record("after")])
    head = await writer.head(topic)
    del writer

    revived = await _maybe(factory())
    assert await revived.head(topic) == head, "head must survive a restart"
    found = await revived.read(topic, after=None, limit=10)
    assert [e.event_id for e in found] == ["before", "after"], (
        "a new handle to the same storage must see what the old one appended"
    )


async def _concurrent_appends_lose_nothing(factory: Factory, topic: str) -> None:
    """Two writers, no lost record and no shared position."""
    log = await _maybe(factory())
    await asyncio.gather(*(
        log.append(topic, [_record(f"w{w}-{i}") for i in range(5)])
        for w in range(4)
    ))

    found = await log.read(topic, after=None, limit=100)
    ids = [e.event_id for e in found]
    positions = [e.position for e in found]

    assert len(ids) == 20, f"concurrent appends lost records: {len(ids)} of 20"
    assert len(set(ids)) == 20, "a record was stored twice"
    assert len(set(positions)) == 20, "two records were given the same position"


async def _a_failed_append_is_invisible(factory: Factory, topic: str) -> None:
    """A crash mid-append must leave nothing readable and no permanent hole.

    Simulated by an append that raises part-way. What must *not* happen is a
    stalled reader: a gap that later records sit behind forever is worse than
    losing the batch, because the batch was never acknowledged to anyone while
    the gap blocks everything after it.
    """
    import contextlib

    log = await _maybe(factory())
    await log.append(topic, [_record("good-1")])

    # A batch whose second element is not a record at all. Any adapter reaching
    # for `.event_id` raises, part-way through, which is the shape of a crash
    # mid-append without needing to reach inside the adapter to cause one.
    with contextlib.suppress(Exception):
        await log.append(topic, [_record("mid-1"), object()])

    await log.append(topic, [_record("good-2")])

    ids = [e.event_id for e in await log.read(topic, after=None, limit=50)]
    assert "good-1" in ids and "good-2" in ids, (
        f"a failed append must not stall later reads; saw {ids}"
    )


async def _retention_never_resurrects(factory: Factory, topic: str) -> None:
    """Whatever retention keeps must stay readable and correctly positioned."""
    log = await _maybe(factory())
    await log.append(topic, [_record(f"e{i}") for i in range(5)])

    # Nothing is old enough for a week-long policy, so nothing may go.
    removed = await log.retain(topic, RetentionPolicy(max_age_seconds=7 * 24 * 3600))
    assert removed == 0, "retention discarded records younger than the policy"

    found = await log.read(topic, after=None, limit=50)
    assert len(found) == 5, "retention removed records it was not allowed to"


async def _wait_for_returns(factory: Factory, topic: str) -> None:
    """It may poll or it may block, but it must return, and it must be honest."""
    log = await _maybe(factory())
    assert await log.wait_for(topic, after=None, timeout=0.05) is False, (
        "wait_for on an empty topic must time out False, not hang or return True"
    )
    await log.append(topic, [_record("arrived")])
    assert await log.wait_for(topic, after=None, timeout=1.0) is True


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


async def verify_checkpoints(factory: Factory, *, topic: str = "conformance") -> None:
    """Assert that *factory* builds a conforming :class:`~loom.events.Checkpoints`."""
    marks = await _maybe(factory())

    assert await marks.load("nobody", topic) is None, (
        "an unknown subscriber must load as None — that is what 'start fresh' means"
    )

    await marks.commit("a", topic, "42")
    assert await marks.load("a", topic) == "42"

    # Independent per subscriber, and per topic. A shared position is how one
    # workflow silently consumes another's backlog.
    await marks.commit("b", topic, "7")
    assert await marks.load("a", topic) == "42", "subscribers must not share a position"
    await marks.commit("a", "other-topic", "99")
    assert await marks.load("a", topic) == "42", "topics must not share a position"

    active = await marks.active(topic)
    assert set(active) == {"a", "b"}, f"active() must list committed subscribers, got {set(active)}"
    assert active["a"].position == "42"
    assert active["a"].updated_at is not None, "a checkpoint must carry when it moved"

    # Moving forward replaces rather than accumulates.
    await marks.commit("a", topic, "50")
    assert await marks.load("a", topic) == "50"

    await marks.forget("a", topic)
    assert await marks.load("a", topic) is None, "forget() must clear the position"
    assert "a" not in await marks.active(topic), "a forgotten subscriber must leave active()"


# ---------------------------------------------------------------------------
# EventSource
# ---------------------------------------------------------------------------


async def verify_event_source(
    source: Any,
    *,
    sign: Callable[[bytes], Mapping[str, str]],
    sample: bytes,
    expected_types: Sequence[str] | None = None,
) -> None:
    """Assert that *source* is a conforming :class:`~loom.events.EventSource`.

    *sign* turns a raw body into the headers a genuine delivery would carry, so
    the kit can produce both an authentic delivery and a tampered one without
    knowing the scheme. *sample* is one real body — copy it out of the
    provider's docs, not out of this code.

    What it checks is deliberately the part an author would not think to test:
    that verification is over the *bytes*, that a tampered body is rejected,
    that the delivery id is stable, and that expansion is pure. The parts an
    author does test — "my parser reads this field" — are their own tests.
    """
    from loom.events.sources import (
        EventSource,
        SourceContext,
        SourceState,
        VerificationFailed,
    )

    assert isinstance(source, EventSource), (
        f"{type(source).__name__} does not satisfy the EventSource protocol; it "
        "needs id, verify, challenge, delivery_id and expand"
    )
    assert getattr(source, "id", ""), "a source needs a non-empty `id`"

    headers = {k.lower(): v for k, v in sign(sample).items()}

    # 1. An authentic delivery verifies.
    source.verify(headers, sample)

    # 2. A tampered body does not — and the headers are left untouched, because
    #    that is the realistic attack: replay a valid signature over new bytes.
    tampered = sample + b" "
    try:
        source.verify(headers, tampered)
    except VerificationFailed:
        pass
    else:
        raise AssertionError(
            "verify() accepted a body that does not match its signature. Every "
            "scheme in use signs the raw bytes; a source that parses first and "
            "signs the re-serialised form accepts anything."
        )

    # 3. A delivery with no signature headers at all is refused. An endpoint
    #    that accepts one is open to the internet, and it looks identical to
    #    one that is not until somebody finds it.
    try:
        source.verify({}, sample)
    except VerificationFailed:
        pass
    else:
        raise AssertionError(
            "verify() accepted a delivery carrying no signature headers. If "
            "that is intentional — a gateway in front verifies — the source "
            "must require an explicit opt-in flag rather than defaulting to it."
        )

    # 4. Identity is stable. A delivery id that changes between two calls on
    #    identical input makes every redelivery a new event.
    import json as _json

    payload = _json.loads(sample)
    first = source.delivery_id(headers, payload)
    second = source.delivery_id(headers, payload)
    assert first == second, (
        f"delivery_id() is not stable: {first!r} then {second!r}. It is the "
        "dedupe key, so an unstable one silently reprocesses everything."
    )

    # 5. Expansion is pure and repeatable, and never invents identity.
    ctx = SourceContext(
        source_id=source.id, state=SourceState(_NullCache(), source.id), headers=headers
    )
    events = list(await source.expand(payload, ctx))
    again = list(await source.expand(payload, ctx))
    assert [e.type for e in events] == [e.type for e in again], (
        "expand() returned different types for the same delivery; it must be a "
        "pure function of the payload, because it is re-run on redelivery"
    )
    for event in events:
        assert event.type, "an InboundEvent needs a type — the topic derives from it"
        assert "." in event.type, (
            f"event type {event.type!r} is not namespaced. It must read "
            "'{source}.{event_type}', because that is what becomes the topic "
            "and what a subscriber declares."
        )
    if expected_types is not None:
        assert [e.type for e in events] == list(expected_types), (
            f"expected {list(expected_types)}, got {[e.type for e in events]}"
        )

    # 6. challenge() answers None for an ordinary delivery. A source returning a
    #    Challenge here would swallow every event as a handshake.
    assert source.challenge(headers, sample) is None, (
        "challenge() must return None for a normal delivery; it is only for a "
        "registration handshake"
    )


class _NullCache:
    """A CacheStore that forgets. Enough for a source that keeps no cursor, and
    a shape-B source under test should be given a real one."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: float = 0) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
