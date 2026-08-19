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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from loom.events.models import EventRecord, RetentionPolicy

__all__ = [
    "verify_checkpoints",
    "verify_effect_profile",
    "verify_event_log",
    "verify_event_source",
    "verify_probe",
    "verify_vector_store",
]

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


def verify_effect_profile(
    manifest: Any,
    *,
    tools_module: Any = None,
    client_source: str | None = None,
) -> None:
    """Assert that *manifest* classifies its own effects consistently.

    The kit a toolset author runs in their own CI. Everything it checks is
    verified against something else the author already wrote, so none of it is
    an opinion about their service:

    * every operation **declares** an effect class, rather than inheriting the
      fail-safe default — which is a backstop, not a classification;
    * declared ``idempotent`` matches the ``@step``'s actual retry policy,
      because those are two sources of truth for one fact and they drift;
    * ``undone_by`` names an operation that exists and agrees with
      ``reversible``, since an id that resolves to nothing produces an
      operation that reads as recoverable and is not;
    * where a client's HTTP verb can be recovered, no declaration contradicts
      it — a ``DELETE`` declared read is how an operation reads as harmless to
      a read-only agent.

    Args:
        manifest: The ``ToolsetManifest`` to check.
        tools_module: The module holding the ``@step`` functions. Defaults to
            importing ``manifest.tools_module``. Pass it explicitly to avoid a
            second import, or when the module is not importable by name.
        client_source: The text of ``client.py``. When given, the verb check
            runs; when omitted it is skipped rather than silently passing —
            a check that cannot run has found nothing.

    Raises:
        AssertionError: naming the operation and what disagrees.
    """
    import importlib

    from loom.toolsets.derive import verbs_in_client, wiring_in_tools
    from loom.toolsets.effects import verb_disagreement

    problems: list[str] = []
    operations = list(manifest.all_operations())

    for op in operations:
        if "effect" not in op.model_fields_set:
            problems.append(
                f"{op.id}: no declared effect class. The default is a fail-safe "
                "backstop, not a classification."
            )
        if op.undone_by:
            if op.undone_by not in {o.id for o in operations}:
                problems.append(
                    f"{op.id}: undone_by={op.undone_by!r} names no operation here"
                )
            if not op.reversible:
                problems.append(
                    f"{op.id}: names an inverse but is not marked reversible, so "
                    "policy will treat it as irreversible"
                )

    module = tools_module
    if module is None and manifest.tools_module:
        try:
            module = importlib.import_module(manifest.tools_module)
        except ImportError:
            module = None

    if module is not None:
        for op in operations:
            fn = getattr(module, op.function, None) if op.function else None
            retry = getattr(fn, "retry", None) if fn is not None else None
            attempts = getattr(retry, "max_attempts", None) if retry else None
            if attempts is not None and op.idempotent != (attempts > 1):
                problems.append(
                    f"{op.id}: declared idempotent={op.idempotent} but its step "
                    f"retries {attempts}x. A non-idempotent operation that "
                    "retries performs it twice."
                )

    if client_source is not None and module is not None:
        verbs = verbs_in_client(client_source)
        source = getattr(module, "__file__", None)
        wiring = {}
        if source:
            try:
                from pathlib import Path

                wiring = wiring_in_tools(Path(source).read_text(encoding="utf-8"))
            except OSError:
                wiring = {}
        for op in operations:
            method = wiring.get(op.function or "")
            verb = verbs.get(method) if method else None
            if verb and (message := verb_disagreement(op, verb)):
                problems.append(message)

    if problems:
        raise AssertionError(
            f"{manifest.id} does not classify its effects consistently:\n  "
            + "\n  ".join(problems)
        )


# ---------------------------------------------------------------------------
# Vector stores
# ---------------------------------------------------------------------------


async def verify_vector_store(
    factory: Factory, *, namespace: str = "conformance"
) -> None:
    """Assert that *factory* builds a conforming ``VectorStore``.

    *factory* is called more than once and **must return a handle to the same
    underlying storage each time** — that is what proves an index survives a
    restart rather than merely a variable.

    What this checks is deliberately the part an author would not think to
    test. That a re-upsert of the same id *updates* rather than appends, so a
    re-run of an ingest does not double the index while looking like it worked.
    That results come back ordered by score with the score attached, because a
    match with no score cannot be thresholded and an unthresholded RAG answer
    is the model citing the least-bad row. That a metadata filter narrows
    *before* ``top_k`` rather than after, so a caller asking for five matching
    rows gets five rather than however many of five happened to match. And that
    an index refuses a second embedding model, because two models occupy two
    different spaces and the arithmetic across them succeeds while meaning
    nothing.

    The parts an author *does* test — "my adapter talks to my database" — are
    their own tests.
    """
    await _vectors_round_trip(factory, namespace)
    await _vectors_upsert_is_an_update(factory, f"{namespace}-upsert")
    await _vectors_rank_by_score(factory, f"{namespace}-rank")
    await _vectors_filter_before_top_k(factory, f"{namespace}-filter")
    await _vectors_empty_namespace_is_empty(factory, f"{namespace}-empty")
    await _vectors_delete_removes(factory, f"{namespace}-delete")
    await _vectors_survive_a_restart(factory, f"{namespace}-restart")
    await _vectors_refuse_a_second_model(factory, f"{namespace}-model")


def _chunk(identifier: str, text: str = "", **metadata: Any) -> Any:
    from loom.knowledge.models import Chunk

    return Chunk(id=identifier, text=text or identifier, metadata=metadata)


def _unit(*components: float) -> list[float]:
    from loom.knowledge.models import normalise

    return normalise(list(components))


async def _vectors_round_trip(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    written = await store.upsert(
        namespace,
        [_chunk("a", "alpha"), _chunk("b", "beta")],
        [_unit(1, 0), _unit(0, 1)],
        model="m1",
    )

    assert written == 2, "upsert must report how many rows it stored"
    assert await store.count(namespace) == 2

    found = await store.query(namespace, _unit(1, 0), top_k=1, model="m1")
    assert len(found) == 1, "top_k must bound the result"
    assert found[0].chunk.id == "a", "the nearest vector must rank first"
    assert found[0].chunk.text == "alpha", "the chunk must round-trip whole"


async def _vectors_upsert_is_an_update(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    await store.upsert(namespace, [_chunk("a", "first")], [_unit(1, 0)], model="m1")
    await store.upsert(namespace, [_chunk("a", "second")], [_unit(1, 0)], model="m1")

    assert await store.count(namespace) == 1, (
        "re-upserting one id must update it. Appending instead doubles the "
        "index on every re-ingest while looking like it worked."
    )
    found = await store.query(namespace, _unit(1, 0), top_k=1, model="m1")
    assert found[0].chunk.text == "second", "the later write must win"


async def _vectors_rank_by_score(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    await store.upsert(
        namespace,
        [_chunk("near"), _chunk("mid"), _chunk("far")],
        [_unit(1, 0), _unit(1, 1), _unit(0, 1)],
        model="m1",
    )

    found = await store.query(namespace, _unit(1, 0), top_k=3, model="m1")
    assert [m.chunk.id for m in found] == ["near", "mid", "far"], (
        "matches must come back ordered by similarity, closest first"
    )
    scores = [m.score for m in found]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1], (
        "the score must be carried. A match with no score cannot be "
        "thresholded, and an unthresholded RAG answer is the model citing the "
        "least-bad row in the index."
    )


async def _vectors_filter_before_top_k(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    await store.upsert(
        namespace,
        [
            _chunk("keep-1", lang="en"),
            _chunk("drop-1", lang="fr"),
            _chunk("drop-2", lang="fr"),
            _chunk("keep-2", lang="en"),
        ],
        [_unit(1, 0), _unit(0.9, 0.1), _unit(0.8, 0.2), _unit(0.7, 0.3)],
        model="m1",
    )

    found = await store.query(
        namespace, _unit(1, 0), top_k=2, where={"lang": "en"}, model="m1"
    )
    assert [m.chunk.id for m in found] == ["keep-1", "keep-2"], (
        "the filter must narrow before top_k, not after. Filtering afterwards "
        "returns however many of the top rows happened to match, which a "
        "caller asking for two matching rows reads as 'only one exists'."
    )


async def _vectors_empty_namespace_is_empty(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    assert await store.count(namespace) == 0
    assert await store.query(namespace, _unit(1, 0), top_k=5, model="m1") == [], (
        "an unknown namespace must answer empty, not raise — a workflow "
        "searching before its first ingest is ordinary."
    )


async def _vectors_delete_removes(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    await store.upsert(
        namespace, [_chunk("a"), _chunk("b")], [_unit(1, 0), _unit(0, 1)], model="m1"
    )

    assert await store.delete(namespace, ["a"]) == 1
    assert await store.count(namespace) == 1

    assert await store.delete(namespace) == 1, (
        "delete with no ids must drop the whole namespace and report how many"
    )
    assert await store.count(namespace) == 0


async def _vectors_survive_a_restart(factory: Factory, namespace: str) -> None:
    first = await _maybe(factory())
    await first.upsert(namespace, [_chunk("a")], [_unit(1, 0)], model="m1")

    second = await _maybe(factory())
    assert await second.count(namespace) == 1, (
        "an index must outlive the handle that wrote it — otherwise every "
        "process restart silently empties it."
    )


async def _vectors_refuse_a_second_model(factory: Factory, namespace: str) -> None:
    store = await _maybe(factory())
    await store.upsert(namespace, [_chunk("a")], [_unit(1, 0)], model="m1")

    try:
        await store.upsert(namespace, [_chunk("b")], [_unit(0, 1)], model="m2")
    except Exception:
        return
    raise AssertionError(
        "a namespace must refuse a vector from a second embedding model. Two "
        "models occupy two different spaces: the arithmetic succeeds, the "
        "ranking is noise, and the scores look entirely ordinary."
    )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


async def verify_probe(
    probe: Any,
    *,
    target: str,
    unsupported: str = "not-a-target://nothing",
    methods_seen: Callable[[], Iterable[str]] | None = None,
) -> None:
    """Assert that *probe* is a conforming :class:`~loom.agents.probes.Probe`.

    *target* is something the probe genuinely handles — point it at a fixture
    you control, not at a third party's site, so a red test means your probe
    broke rather than someone else's server did.

    Pass *methods_seen* when the target can report how it was accessed: a local
    server that records request methods, say. That is the only mechanical proof
    of the property that matters most here, and the one an author is least
    likely to write a test for, because the code obviously does not write —
    right up until a redirect, a retry, or a helpfully-added POST fallback means
    it does. A probe is handed to a model; read-only has to be demonstrable.

    What it does not check: what your probe *reports*. That is your test, and it
    is the part you will remember to write.
    """
    from loom.agents.probes.base import Observation, ProbeError

    if not isinstance(getattr(probe, "id", None), str) or not probe.id:
        raise AssertionError("a probe needs a non-empty string id to be selected by")

    if not probe.supports(target):
        raise AssertionError(
            f"{probe.id} was asked to verify against {target!r} and says it does "
            "not support it. Pass a target this probe handles."
        )

    if probe.supports(unsupported):
        raise AssertionError(
            f"{probe.id}.supports({unsupported!r}) is True. A probe that claims "
            "everything is chosen for targets it cannot read, and its failure "
            "surfaces as an error rather than as a different probe being picked."
        )

    for junk in ("", "   ", "://", "\x00"):
        try:
            probe.supports(junk)
        except Exception as exc:
            raise AssertionError(
                f"{probe.id}.supports({junk!r}) raised {exc!r}. Selection runs "
                "over every registered probe, so one that raises here removes "
                "the others from consideration too."
            ) from exc

    try:
        first = await probe.observe(target, hint="conformance")
    except ProbeError:
        raise
    except Exception as exc:
        raise AssertionError(
            f"{probe.id} raised {type(exc).__name__} rather than ProbeError. The "
            "caller distinguishes 'could not look' from 'the code is wrong', and "
            "collapsing the two puts a model to work repairing nothing."
        ) from exc

    if not isinstance(first, Observation):
        raise AssertionError(f"{probe.id}.observe must return an Observation")
    if first.target != target:
        raise AssertionError(
            f"{probe.id} reported target {first.target!r} for {target!r}; an "
            "observation is attributed to what was asked for."
        )
    if not first.summary.strip():
        raise AssertionError(
            f"{probe.id} returned an empty summary. The summary is the part a "
            "model reads first and sometimes the only part it reads."
        )
    if first.probe and first.probe != probe.id:
        raise AssertionError(
            f"{probe.id} attributed its observation to {first.probe!r}"
        )

    second = await probe.observe(target, hint="conformance")
    if not second.summary.strip():
        raise AssertionError(f"{probe.id} returned nothing on a second look")

    if methods_seen is not None:
        used = {m.upper() for m in methods_seen()}
        if not used <= {"GET", "HEAD"}:
            raise AssertionError(
                f"{probe.id} used {sorted(used - {'GET', 'HEAD'})}. Looking must "
                "not change anything: authoring runs against systems the author "
                "has not agreed to let a model write to."
            )
