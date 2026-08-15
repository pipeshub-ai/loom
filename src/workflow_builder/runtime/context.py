"""The durable execution context.

``Context`` is the only legal doorway from deterministic orchestration code to the
non-deterministic world. Every method on it either journals its result or is a pure
function of already-journaled state, which is what lets an execution be replayed after a
crash, a deploy, or a three-week wait for a human to click "approve".
"""

from __future__ import annotations

import asyncio
import json
import logging
import random as _random
import warnings
from collections.abc import Awaitable, Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeVar, overload

from workflow_builder.agents.memory import replace_history
from workflow_builder.agents.result import AgentResult
from workflow_builder.connectors.credentials import credential_store_scope
from workflow_builder.core.exceptions import (
    AuthExpired,
    BudgetExceeded,
    ConfigurationError,
    ContinueAsNew,
    ControlSignal,
    RetriesExhausted,
    SerializationError,
    StepError,
    Suspend,
    TimeoutExceeded,
    WorkflowCancelled,
)
from workflow_builder.core.ids import fingerprint as make_fingerprint
from workflow_builder.core.models import ErrorInfo, Usage
from workflow_builder.core.retry import Failure, OnError, Retry
from workflow_builder.core.serde import decode, encode
from workflow_builder.core.types import DepsT, Duration, to_seconds
from workflow_builder.runtime.determinism import step_scope
from workflow_builder.runtime.effects import EffectCall, EffectDenied
from workflow_builder.runtime.journal import (
    EntryKind,
    EntryStatus,
    Journal,
    JournalEntry,
    Scope,
)
from workflow_builder.security.authority import Authority
from workflow_builder.steps.context import StepContext
from workflow_builder.steps.definition import StepDefinition
from workflow_builder.storage.artifact import ArtifactVersion
from workflow_builder.storage.attachment import Attachment
from workflow_builder.toolsets.manifest import EffectClass

if TYPE_CHECKING:
    from workflow_builder.agents.agent import Agent
    from workflow_builder.core.models import ExecutionRecord
    from workflow_builder.observability.tracing import Span
    from workflow_builder.runtime.engine import Runtime
    from workflow_builder.runtime.workflow import WorkflowDefinition

T = TypeVar("T")


#: How a journal entry kind reads to a broker. The broker's vocabulary is
#: coarser on purpose: it decides authority, and authority is granted over
#: toolsets, agents, and sub-workflows rather than over journal mechanics.
_EFFECT_KINDS: dict[EntryKind, str] = {
    EntryKind.STEP: "step",
    EntryKind.AGENT: "agent",
    EntryKind.CHILD_WORKFLOW: "child",
    EntryKind.TOOL_CALL: "tool",
}


def _effect_arguments(recorded: Any) -> dict[str, Any]:
    """The call's arguments, as far as a policy can usefully see them.

    Best-effort by design. A broker decides on *what* is being called far more
    often than on the values passed to it, and a step invoked positionally has
    no argument names to report — so this offers what it can and never fails a
    call over presentation.
    """
    if isinstance(recorded, dict) and "kwargs" in recorded:
        named = recorded.get("kwargs")
        return dict(named) if isinstance(named, dict) else {}
    return dict(recorded) if isinstance(recorded, dict) else {}


class DurableCall(Generic[T]):
    """A durable operation whose journal path is fixed at construction time.

    Allocating the path eagerly — synchronously, when you call ``ctx.step(...)`` rather
    than when the coroutine starts — is what keeps concurrent calls stably identified
    across replays even though they complete out of order.
    """

    __slots__ = (
        "_ctx",
        "_fallback",
        "_fingerprint",
        "_future",
        "_input",
        "_metadata",
        "_on_error",
        "_output_type",
        "_perform",
        "_retry",
        "_timeout",
        "kind",
        "name",
        "path",
    )

    def __init__(
        self,
        ctx: Context[Any],
        *,
        kind: EntryKind,
        name: str,
        perform: Callable[[int, StepContext], Awaitable[Any]],
        fingerprint: str = "",
        input: Any = None,
        output_type: Any = None,
        retry: Retry | None = None,
        timeout: Duration | None = None,
        on_error: OnError = OnError.RAISE,
        fallback: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._ctx = ctx
        self.path = ctx._scope.allocate()
        self.kind = kind
        self.name = name
        self._perform = perform
        self._fingerprint = fingerprint
        self._input = input
        self._output_type = output_type
        self._retry = retry or Retry(max_attempts=1)
        self._timeout = timeout
        self._on_error = on_error
        self._fallback = fallback
        self._metadata = metadata or {}
        self._future: asyncio.Future[Any] | None = None

    def __await__(self) -> Generator[Any, None, T]:
        """Awaiting yields the call's own type, not ``Any``.

        ``-> Any`` made every ``await ctx.step(...)`` and ``await ctx.agent(...)``
        untyped at the point of use, so a workflow returning the result from a
        ``-> str`` body drew ``no-any-return`` from mypy — on the exact pattern
        the coding agent is told to write. The generic was declared and then
        discarded one method later.
        """
        return self._task().__await__()

    def _task(self) -> asyncio.Future[T]:
        """Memoize execution so awaiting the same call twice does not run it twice."""
        if self._future is None:
            self._future = asyncio.ensure_future(self._resolve())
        return self._future

    async def _resolve(self) -> Any:
        ctx = self._ctx
        journal = ctx._journal

        recorded = journal.lookup(self.path, self.kind, self.name)
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            return decode(await ctx._load_payload(recorded.output), self._output_type)
        if recorded is not None and recorded.status is EntryStatus.FAILED:
            # Replay must surface the failure the workflow originally saw. To re-run the
            # step against fixed code use runtime.retry(), which prunes failed entries.
            return self._handle_failure(
                StepError(
                    recorded.error.message if recorded.error else "step failed",
                    step_name=self.name,
                    attempts=recorded.attempts,
                ),
                attempts=recorded.attempts,
            )

        entry = JournalEntry(
            path=self.path,
            kind=self.kind,
            name=self.name,
            fingerprint=self._fingerprint,
            input=_encode_debug(self._input),
            status=EntryStatus.PENDING,
            started_at=self._clock.now(),
            metadata={k: v for k, v in self._metadata.items() if v is not None},
        )
        journal.put(entry)

        span = ctx._tracer.start_span(
            f"{self.kind.value}.{self.name}",
            attributes={"workflow": ctx.workflow, "run_id": ctx.run_id, "path": self.path},
        )
        try:
            outcome = await ctx._runtime.broker.dispatch(
                self._effect_call(entry, span), ctx._authority
            )
        except ControlSignal:
            span.end()
            raise
        except BaseException:
            span.end()
            raise
        if outcome.ok:
            span.end()
            return outcome.value

        # Refused. Journal it as a failure so a replay sees what the run saw —
        # a denial that left no trace would replay as if the effect had never
        # been attempted, and the second run would take a different path.
        span.end()
        refusal = EffectDenied(
            outcome.error or f"effect '{self.name}' denied",
            call=self._effect_call(entry, span),
            needs=outcome.needs,
        )
        entry.status = EntryStatus.FAILED
        entry.error = ErrorInfo.from_exception(refusal, step_name=self.name)
        entry.finished_at = self._clock.now()
        journal.put(entry)
        await ctx._maybe_flush(force=True)
        return self._handle_failure(refusal, attempts=entry.attempts)

    def _effect_call(self, entry: JournalEntry, span: Span) -> EffectCall:
        """Describe this operation for the broker, and how to carry it out."""

        async def perform() -> Any:
            return await self._attempt_loop(entry, span)

        return EffectCall(
            kind=_EFFECT_KINDS.get(self.kind, self.kind.value),
            target=self._metadata.get("effect_target") or self.name,
            arguments=_effect_arguments(self._input),
            effect=self._metadata.get("effect_class") or EffectClass.WRITE,
            run_id=self._ctx.run_id,
            path=self.path,
            perform=perform,
        )

    @property
    def _clock(self) -> Any:
        return self._ctx._clock

    async def _attempt_loop(self, entry: JournalEntry, span: Span) -> Any:
        """Run the operation, retrying per its policy, and journal the outcome."""
        ctx = self._ctx
        journal = ctx._journal
        attempt = 0
        last_error: BaseException | None = None
        while True:
            attempt += 1
            entry.attempts = attempt
            step_ctx = StepContext(
                run_id=ctx.run_id,
                workflow=ctx.workflow,
                step_name=self.name,
                path=self.path,
                attempt=attempt,
                max_attempts=self._retry.max_attempts,
                deps=ctx.deps,
                idempotency_key=f"{ctx.run_id}:{self.path}",
                logger=ctx.logger,
                credentials=ctx._runtime.credentials,
                span=span,
            )
            try:
                # Bound for the duration of this attempt so a toolset's
                # process-wide client singleton (get_default_client(),
                # get_default_auth()) can resolve this run's CredentialStore
                # with no `ctx` parameter of its own — see
                # connectors/credentials.py::current_credential_store().
                with credential_store_scope(ctx._runtime.credentials):
                    result = await self._invoke(attempt, step_ctx)
            except ControlSignal:
                raise
            except AuthExpired:
                # Handled like a ControlSignal, not a step failure: writing
                # this attempt to the journal as FAILED would make it
                # permanent (see the FAILED branch in DurableCall._resolve —
                # a replay must reproduce a recorded failure exactly, which
                # is correct for an ordinary bug but wrong for a credential
                # that becomes valid again after 'loom connect'). Left
                # PENDING instead, propagating past this loop's own
                # journal.put(entry) so the engine's Suspend conversion in
                # runtime/engine.py can park the run, and a later resume
                # actually re-attempts this step rather than replaying a
                # baked-in failure.
                raise
            except (TimeoutError, Exception) as exc:
                last_error = exc
                if not self._retry.should_retry(exc, attempt):
                    break
                ctx.logger.warning(
                    "step %s failed (attempt %d/%d): %s",
                    self.name,
                    attempt,
                    self._retry.max_attempts,
                    exc,
                )
                await ctx._clock.sleep(self._retry.delay_for(attempt, rng=ctx._backoff_rng))
            else:
                entry.status = EntryStatus.COMPLETED
                entry.output = await ctx._store_payload(
                    _encode_durable(
                        result, what=f"{self.kind.value} '{self.name}' returned a value"
                    )
                )
                entry.finished_at = self._clock.now()
                if isinstance(usage := getattr(result, "usage", None), Usage):
                    entry.usage = usage
                journal.put(entry)
                span.set_status("ok")
                await ctx._maybe_flush()
                return result

        assert last_error is not None
        entry.status = EntryStatus.FAILED
        entry.error = ErrorInfo.from_exception(last_error, step_name=self.name)
        entry.finished_at = self._clock.now()
        journal.put(entry)
        span.record_exception(last_error)
        await ctx._maybe_flush(force=True)

        surfaced: BaseException = last_error
        if attempt > 1:
            surfaced = RetriesExhausted(
                f"step '{self.name}' failed after {attempt} attempts: {last_error}",
                step_name=self.name,
                attempts=attempt,
                cause=last_error,
            )
        return self._handle_failure(surfaced, attempts=attempt)

    async def _invoke(self, attempt: int, step_ctx: StepContext) -> Any:
        ctx = self._ctx
        limiter = ctx._runtime.limiter_for(self.name, self._metadata.get("concurrency_key"))
        timeout = to_seconds(self._timeout) if self._timeout is not None else None

        async def body() -> Any:
            with step_scope():
                return await self._perform(attempt, step_ctx)

        if limiter is not None:
            async with limiter:
                return await _with_timeout(body(), timeout, self.name)
        return await _with_timeout(body(), timeout, self.name)

    def _handle_failure(self, error: BaseException, *, attempts: int) -> Any:
        """Apply the step's ``on_error`` mode once retries are exhausted."""
        if self._on_error is OnError.CONTINUE:
            self._ctx.logger.warning("step %s failed, continuing: %s", self.name, error)
            return self._fallback
        if self._on_error is OnError.ROUTE:
            return Failure(
                step=self.name,
                error_type=type(error).__name__,
                message=str(error),
                attempts=attempts,
            )
        raise error

    def __repr__(self) -> str:
        return f"<DurableCall {self.path} {self.kind.value}:{self.name}>"


async def _with_timeout(awaitable: Awaitable[Any], timeout: float | None, name: str) -> Any:
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError as exc:
        raise TimeoutExceeded(f"step '{name}' exceeded its {timeout}s timeout") from exc


def _encode_durable(value: Any, *, what: str) -> Any:
    """Encode a value that will be **served back on replay**.

    This must never degrade. A durable value that silently became a placeholder
    would be handed to the workflow on the next attempt as if it were real, so a
    value that cannot be journaled is an error, not a warning.
    """
    try:
        return encode(value)
    except SerializationError as exc:
        raise SerializationError(f"{what}: {exc}") from exc


def _encode_debug(value: Any) -> Any:
    """Encode a value recorded only for observability.

    Step inputs and signal payloads are written for humans reading a trace; they
    are never replayed into the workflow. Degrading here loses a debugging aid,
    not correctness, so a stubborn value is marked rather than fatal.
    """
    try:
        return encode(value)
    except Exception:
        return {"__unserializable__": type(value).__name__}


#: Marker wrapping a journal payload that was offloaded to blob storage.
BLOB_KEY = "__blob__"


def _is_blob_marker(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {BLOB_KEY}


def _json_bytes(encoded: Any) -> bytes | None:
    """Serialize an already-encoded payload for sizing, or None if it will not."""
    try:
        return json.dumps(encoded).encode()
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class WorkflowState:
    """``ctx.state`` — the workflow's own key-value space, already scoped.

    A thin binding rather than a store of its own: it exists so a workflow body
    never has to name its own workflow, which is the detail most likely to be
    got wrong and least likely to be noticed when it is.
    """

    _ctx: Context[Any]

    async def get(self, key: str, default: Any = None) -> Any:
        found = await self._store.get(self._workflow, key)
        return default if found is None else found

    async def set(self, key: str, value: Any) -> None:
        await self._store.set(self._workflow, key, value)

    async def delete(self, key: str) -> None:
        await self._store.delete(self._workflow, key)

    async def keys(self) -> list[str]:
        return await self._store.keys(self._workflow)

    @property
    def _store(self) -> Any:
        return self._ctx._runtime.state

    @property
    def _workflow(self) -> str:
        return self._ctx.workflow


def _authority_for(
    runtime: Runtime,
    definition: WorkflowDefinition[Any, Any, Any],
) -> Authority:
    """What a run of *definition* on *runtime* is permitted to do.

    Note what is *not* here: the workflow's declared grants never override the
    Runtime's. They are consulted only when the Runtime declared none, where
    they act as a self-limitation. A workflow that could widen its own
    permissions by asking for more would make the declaration worthless.
    """
    base = runtime.authority or Authority()
    if base.grant.is_empty and definition.grants is not None:
        return base.narrowed(grant=definition.grants)
    return base


class Context(Generic[DepsT]):
    """Durable orchestration API, passed as the first argument to every workflow."""

    def __init__(
        self,
        *,
        runtime: Runtime,
        record: ExecutionRecord,
        journal: Journal,
        definition: WorkflowDefinition[Any, Any, DepsT],
        deps: DepsT | None = None,
        logger: logging.Logger | None = None,
        scope: Scope | None = None,
    ) -> None:
        self._runtime = runtime
        self._record = record
        self._journal = journal
        self._definition = definition
        self._deps = deps
        self._scope = scope or journal.root
        self.logger = logger or logging.getLogger(f"workflow.{record.workflow}")
        self._tracer = runtime.tracer
        self._clock = runtime.clock
        """Every timestamp this run writes and every wait it holds in memory.

        Bound once rather than read through the Runtime each time, so a test
        that swaps the clock does so before the run starts and cannot change it
        underneath a run already in flight."""
        self._authority = _authority_for(runtime, definition)
        """What this run may do, handed to the broker on every dispatch.

        The Runtime's authority, falling back to what the workflow declared it
        needs — a workflow can narrow itself, but nothing it says can widen the
        Runtime."""
        self._backoff_rng = _random.Random(record.run_id)
        self._pending_flush = 0
        self._compensation_stack: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...]]] = []
        self._active_patches: frozenset[str] = frozenset()
        self._warned_journal_size = False

    def nested(self, path: str) -> Context[DepsT]:
        """A view of this context whose durable calls nest beneath ``path``.

        Composite operations (an agent loop, a pattern helper) use this so their internal
        model and tool calls get their own numbering space.
        """
        clone = Context(
            runtime=self._runtime,
            record=self._record,
            journal=self._journal,
            definition=self._definition,
            deps=self._deps,
            logger=self.logger,
            scope=self._scope.child(path),
        )
        return clone

    # -- identity ---------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._record.run_id

    @property
    def workflow(self) -> str:
        return self._record.workflow

    @property
    def attempt(self) -> int:
        """How many times the orchestration body has been entered, replays included."""
        return self._record.attempt

    @property
    def deps(self) -> DepsT:
        return self._deps  # type: ignore[return-value]

    @property
    def usage(self) -> Usage:
        """Token and cost totals accumulated by this run so far."""
        return self._journal.total_usage()

    # -- durable work -----------------------------------------------------------------

    def step(
        self,
        target: StepDefinition[Any, T] | Callable[..., Awaitable[T]],
        /,
        *args: Any,
        name: str | None = None,
        retry: Retry | int | None = None,
        timeout: Duration | None = None,
        on_error: OnError | None = None,
        fallback: Any = None,
        **kwargs: Any,
    ) -> DurableCall[T]:
        """Run a step durably: journaled, retried, and skipped entirely on replay.

        Accepts either a ``@step``-decorated definition or a bare async function, so a
        one-off side effect does not need ceremony.
        """
        definition = (
            target
            if isinstance(target, StepDefinition)
            else StepDefinition(fn=target, name=name or getattr(target, "__name__", "anonymous"))
        )
        effective_retry = (
            definition.retry
            if retry is None
            else Retry(max_attempts=retry)
            if isinstance(retry, int)
            else retry
        )
        step_name = name or definition.name
        cache = definition.cache

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            if cache is None:
                return await definition.invoke(step_ctx, *args, **kwargs)

            key = (
                cache.key(*args, **kwargs)
                if cache.key
                else make_fingerprint(step_name, args, kwargs)
            )
            scoped = f"step:{step_name}:{key}" if cache.scope == "global" else (
                f"step:{self.run_id}:{step_name}:{key}"
            )
            hit = await self._runtime.cache.get(scoped)
            if hit is not None:
                step_ctx.metadata["cache_hit"] = True
                return decode(hit, definition.output_type)
            produced = await definition.invoke(step_ctx, *args, **kwargs)
            await self._runtime.cache.set(scoped, encode(produced), to_seconds(cache.ttl))
            return produced

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=step_name,
            perform=perform,
            fingerprint=make_fingerprint(step_name, args, kwargs),
            input={"args": args, "kwargs": kwargs},
            output_type=definition.output_type,
            retry=effective_retry,
            timeout=definition.timeout if timeout is None else timeout,
            on_error=on_error or definition.on_error,
            fallback=definition.fallback if fallback is None else fallback,
            metadata={"concurrency_key": definition.concurrency_key},
        )

    def call(
        self,
        name: str,
        fn: Callable[[], Awaitable[T]],
        *,
        retry: Retry | int | None = None,
        timeout: Duration | None = None,
        on_error: OnError = OnError.RAISE,
        fallback: Any = None,
    ) -> DurableCall[T]:
        """Journal an inline closure. Handy for adapting a third-party client in place."""
        effective_retry = (
            Retry(max_attempts=retry) if isinstance(retry, int) else retry or Retry(max_attempts=1)
        )

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            return await fn()

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=name,
            perform=perform,
            fingerprint=make_fingerprint(name),
            retry=effective_retry,
            timeout=timeout,
            on_error=on_error,
            fallback=fallback,
        )

    # -- deterministic reads of non-deterministic sources -----------------------------

    def _side_effect(self, name: str, produce: Callable[[], Any]) -> Any:
        """Record a value once and replay it verbatim thereafter."""
        path = self._scope.allocate()
        recorded = self._journal.lookup(path, EntryKind.SIDE_EFFECT, name)
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            return recorded.output

        value = _encode_durable(produce(), what=f"side effect '{name}'")
        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.SIDE_EFFECT,
                name=name,
                status=EntryStatus.COMPLETED,
                output=value,
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
                attempts=1,
            )
        )
        self._pending_flush += 1
        return value

    def now(self) -> datetime:
        """The current UTC time, frozen into the journal so replays agree."""
        recorded = self._side_effect("now", lambda: self._clock.now().isoformat())
        return datetime.fromisoformat(recorded)

    def uuid4(self) -> str:
        """A random UUID that stays the same across replays."""
        import uuid as _uuid

        return str(self._side_effect("uuid4", lambda: str(_uuid.uuid4())))

    def random(self) -> _random.Random:
        """A seeded RNG. The seed is journaled, so the whole sequence is reproducible."""
        return _random.Random(self._side_effect("random_seed", lambda: _random.getrandbits(64)))

    # -- time and events --------------------------------------------------------------

    async def sleep(self, duration: Duration, *, name: str = "sleep") -> None:
        """Durably pause. Short waits idle in process; long ones release the worker.

        Unlike ``asyncio.sleep`` this survives a restart: the wake time is journaled, so a
        run parked for thirty days resumes on a machine that did not exist when it began.
        """
        await self.sleep_until(self.now() + timedelta(seconds=to_seconds(duration)), name=name)

    async def sleep_until(self, when: datetime, *, name: str = "sleep") -> None:
        """Durably pause until a specific moment.

        The absolute-time counterpart to :meth:`sleep`. Prefer it for "9am on the
        first of the month" — a duration computed from ``now`` drifts every time
        the run is re-entered, an absolute wake time does not.
        """
        path = self._scope.allocate()
        recorded = self._journal.lookup(path, EntryKind.SLEEP, name)
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            return

        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)

        entry = recorded or JournalEntry(
            path=path,
            kind=EntryKind.SLEEP,
            name=name,
            status=EntryStatus.SUSPENDED,
            started_at=self._clock.now(),
        )
        entry.wake_at = when
        self._journal.put(entry)

        remaining = (when - self._clock.now()).total_seconds()
        if remaining <= 0:
            entry.status = EntryStatus.COMPLETED
            entry.finished_at = self._clock.now()
            self._journal.put(entry)
            return

        if remaining <= self._runtime.inline_timer_threshold:
            # Short waits are cheaper to hold in memory than to park and rehydrate.
            await self._flush()
            await self._clock.sleep(remaining)
            entry.status = EntryStatus.COMPLETED
            entry.finished_at = self._clock.now()
            self._journal.put(entry)
            return

        await self._flush()
        raise Suspend(f"sleeping until {when.isoformat()}", path=path, wake_at=when)

    async def wait_for_event(
        self,
        name: str,
        *,
        timeout: Duration | None = None,
        output_type: Any = None,
        default: Any = None,
    ) -> Any:
        """Park until an external event arrives, then return its payload.

        This is the primitive behind webhooks that resume a run, human approvals, and
        callbacks from long-running third-party jobs. Returns ``default`` on timeout.
        """
        path = self._scope.allocate()
        recorded = self._journal.lookup(path, EntryKind.EVENT, name)
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            return decode(recorded.output, output_type)

        deadline = (
            recorded.wake_at
            if recorded is not None and recorded.wake_at is not None
            else (self.now() + timedelta(seconds=to_seconds(timeout)) if timeout else None)
        )

        delivered = await self._runtime.take_event(self.run_id, name)
        if delivered is not None:
            payload = _encode_durable(delivered.payload, what=f"event '{name}' payload")
            self._journal.put(
                JournalEntry(
                    path=path,
                    kind=EntryKind.EVENT,
                    name=name,
                    status=EntryStatus.COMPLETED,
                    output=payload,
                    started_at=self._clock.now(),
                    finished_at=self._clock.now(),
                )
            )
            await self._maybe_flush()
            return decode(payload, output_type)

        if deadline is not None and deadline <= self._clock.now():
            self._journal.put(
                JournalEntry(
                    path=path,
                    kind=EntryKind.EVENT,
                    name=name,
                    status=EntryStatus.COMPLETED,
                    output=_encode_durable(default, what=f"event '{name}' timeout default"),
                    metadata={"timed_out": True},
                    started_at=self._clock.now(),
                    finished_at=self._clock.now(),
                )
            )
            await self._maybe_flush()
            return default

        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.EVENT,
                name=name,
                status=EntryStatus.SUSPENDED,
                wake_at=deadline,
                started_at=self._clock.now(),
            )
        )
        await self._flush()
        raise Suspend(
            f"waiting for event '{name}'",
            path=path,
            wake_at=deadline,
            awaiting_event=name,
        )

    async def wait_for_approval(
        self,
        subject: str,
        *,
        timeout: Duration | None = None,
        on_timeout: str = "reject",
    ) -> bool:
        """Block on a human decision, identified by ``subject``.

        The run costs nothing while parked. Deliver the answer with
        ``runtime.approve(run_id, subject)``.
        """
        answer = await self.wait_for_event(
            f"approval:{subject}", timeout=timeout, default={"approved": on_timeout == "approve"}
        )
        if isinstance(answer, dict):
            return bool(answer.get("approved", False))
        return bool(answer)

    async def signal(self, run_id: str, name: str, payload: Any = None) -> None:
        """Send an event to another execution, journaled so it fires exactly once."""

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            await self._runtime.send_event(run_id, name, payload)
            return True

        await DurableCall(
            self,
            kind=EntryKind.SIGNAL,
            name=f"signal:{name}",
            perform=perform,
            input={"run_id": run_id, "payload": payload},
        )

    async def publish(self, name: str, payload: Any = None) -> None:
        """Broadcast an event to whoever is waiting for it.

        The counterpart to :meth:`signal`: that one names a target run, this one
        does not, so any run parked on ``wait_for_event(name)`` may take it.
        Journaled, so a replay does not publish a second time.

        Use it for "this happened" — an order shipped, a document indexed — where
        the publisher has no business knowing who cares.

        Named ``publish`` rather than ``emit`` because "emit" is also the
        natural word for streaming a run's output, which is :meth:`report`.
        Two meanings under one name is the kind of ambiguity that produces code
        which reads correctly and does the other thing.
        """

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            await self._runtime.send_event(None, name, payload)
            return True

        await DurableCall(
            self,
            kind=EntryKind.SIGNAL,
            name=f"emit:{name}",
            perform=perform,
            input={"payload": payload},
        )

    async def emit(self, name: str, payload: Any = None) -> None:
        """Deprecated alias for :meth:`publish`.

        The journal entry keeps its ``emit:`` prefix, so a run started under the
        old name replays under the new one. Renaming the entry would have made
        every in-flight run's journal unreadable to the code that has to finish
        it, which is a steep price for a tidier string.
        """
        warnings.warn(
            "ctx.emit() is deprecated; use ctx.publish() for events, or "
            "ctx.report() to stream a run's output. Removed in the next minor.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.publish(name, payload)

    # -- talking about a run while it runs ---------------------------------------------

    async def report(self, message: str, *, kind: str = "text") -> None:
        """Say what this run is doing, for anyone watching.

        Not journaled, and deliberately so. A report is an observation about a
        run, not a durable operation of it: journaling one would make progress
        chatter part of the replay contract, so a workflow could not be made
        more talkative without changing what its replays produce.

        The consequence is that a replay reports again, since the body really
        does run. That is the right outcome — a replay is a real execution and
        someone watching it should see it move — and it cannot be mistaken for
        the original, because the reports carry the replay's own run id.
        """
        await self._runtime.stream.report(self.run_id, message, kind=kind)

    @property
    def state(self) -> WorkflowState:
        """Key-value state shared by every run of this workflow.

        Scoped to the workflow, and mutable — where an artifact is immutable
        and versioned. Reach for it when a run needs to know what the last run
        left behind::

            since = await ctx.state.get("cursor", default=0)
            ...
            await ctx.state.set("cursor", newest)

        Reads and writes are *not* journaled, because the value is shared: a
        replay that served the original value would be reading a fact about the
        past and calling it the present. What that means in practice is that
        state is not deterministic across replays, and a workflow whose control
        flow branches on it will not replay identically. Journal the decision —
        put the read inside a step — when that matters.
        """
        return WorkflowState(self)

    # -- composition ------------------------------------------------------------------

    async def gather(
        self,
        *calls: Awaitable[Any],
        max_concurrency: int | None = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Await durable calls concurrently while preserving replay determinism.

        Sibling failures do not cancel each other: every branch settles so its journal
        entry becomes durable, and only then is the first error re-raised.
        """
        items = list(calls)
        if not items:
            return []

        semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None

        async def guarded(awaitable: Awaitable[Any]) -> Any:
            if semaphore is None:
                return await awaitable
            async with semaphore:
                return await awaitable

        tasks = [asyncio.ensure_future(guarded(item)) for item in items]
        results: list[Any] = []
        suspensions: list[BaseException] = []
        failures: list[BaseException] = []

        for task in tasks:
            try:
                results.append(await task)
            except ControlSignal as signal:
                suspensions.append(signal)
                results.append(None)
            except Exception as exc:
                if return_exceptions:
                    results.append(exc)
                else:
                    failures.append(exc)
                    results.append(None)

        if suspensions:
            raise suspensions[0]
        if failures:
            raise failures[0]
        return results

    async def map(
        self,
        target: StepDefinition[Any, T] | Callable[..., Awaitable[T]],
        items: Iterable[Any],
        *,
        max_concurrency: int = 10,
        return_exceptions: bool = False,
        **kwargs: Any,
    ) -> list[T]:
        """Fan out one step across many inputs with a concurrency ceiling.

        Explicit and bounded, unlike implicit per-item mapping — the source of both n8n's
        item-linking errors and its memory blowups.
        """
        calls = [self.step(target, item, **kwargs) for item in items]
        return await self.gather(
            *calls, max_concurrency=max_concurrency, return_exceptions=return_exceptions
        )

    @staticmethod
    def batched(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
        """Split a sequence into fixed-size chunks: the sane Loop Over Items."""
        return [items[index : index + size] for index in range(0, len(items), size)]

    # -- sub-workflows and agents -----------------------------------------------------

    def child(
        self,
        workflow: WorkflowDefinition[Any, T, Any] | str,
        input: Any = None,
        *,
        name: str | None = None,
        detached: bool = False,
    ) -> DurableCall[T]:
        """Invoke another workflow as a durable child.

        ``detached=True`` starts it and returns the run id immediately, the equivalent of
        a fire-and-forget sub-workflow.
        """
        definition = self._runtime.resolve_workflow(workflow)
        label = name or definition.name

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            if detached:
                return await self._runtime.submit(
                    definition, input, parent_run_id=self.run_id, deps=self._deps
                )
            result = await self._runtime.run(
                definition,
                input,
                parent_run_id=self.run_id,
                root_run_id=self._record.root_run_id or self.run_id,
                deps=self._deps,
            )
            return result.unwrap()

        return DurableCall(
            self,
            kind=EntryKind.CHILD_WORKFLOW,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (input,)),
            input=input,
            output_type=None if detached else definition.output_type,
        )

    @overload
    def agent(
        self,
        agent_or_prompt: str,
        input: Any = ...,
        *,
        name: str | None = ...,
        max_turns: int | None = ...,
        session_id: str | None = ...,
        agent_id: str = ...,
        toolsets: list[str] | None = ...,
        **kwargs: Any,
    ) -> DurableCall[AgentResult[str]]: ...

    @overload
    def agent(
        self,
        agent_or_prompt: Agent[Any, T],
        input: Any = ...,
        *,
        name: str | None = ...,
        max_turns: int | None = ...,
        session_id: str | None = ...,
        agent_id: str = ...,
        toolsets: list[str] | None = ...,
        **kwargs: Any,
    ) -> DurableCall[AgentResult[T]]: ...

    def agent(
        self,
        agent_or_prompt: Agent[Any, T] | str,
        input: Any = None,
        *,
        name: str | None = None,
        max_turns: int | None = None,
        session_id: str | None = None,
        agent_id: str = "",
        toolsets: list[str] | None = None,
        **kwargs: Any,
    ) -> DurableCall[AgentResult[T]]:
        """Run an agent durably.

        Two calling conventions:

        1. **Prompt-only** — ``await ctx.agent("Find AI articles")``
           Uses the ``agent_backend`` configured on the Runtime.
           Tools are resolved from ``rt.toolsets``.

        2. **Agent object** — ``await ctx.agent(my_agent, "input")``
           Uses the Agent's own executor (backward compatible).

        Parameters
        ----------
        toolsets:
            Optional list of toolset IDs to resolve from the registry.
            If None, all registered toolsets are used.
        session_id:
            Conversation to continue. Prior turns are replayed into the call and
            the updated transcript is stored afterwards, so calling the same
            agent twice in one workflow is a conversation rather than two
            unrelated one-shots. Raises if the backend cannot honour it.
        agent_id:
            Stable identity for the agent. Memory is keyed by
            ``agent_id:session_id``, so two agents sharing a session id keep
            separate memories of it.
        max_turns:
            Per-call override of the backend's turn budget.
        """
        if isinstance(agent_or_prompt, str) and input is None:
            return self._agent_from_backend(
                agent_or_prompt,
                name=name,
                toolsets=toolsets,
                session_id=session_id,
                max_turns=max_turns,
                agent_id=agent_id,
            )

        # Backward-compatible path: Agent object
        from workflow_builder.agents.runner import run_agent_durably

        agent = agent_or_prompt
        label = name or f"agent:{agent.name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            return await run_agent_durably(
                agent,
                input,
                ctx=self.nested(step_ctx.path),
                max_turns=max_turns,
                session_id=session_id,
                **kwargs,
            )

        return DurableCall(
            self,
            kind=EntryKind.AGENT,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (input,)),
            input=input,
            # Without this the journalled result decodes back as a plain dict on
            # replay, and `result.output` raises AttributeError on the second
            # attempt but not the first.
            output_type=AgentResult,
            retry=Retry(max_attempts=1),
        )

    def _agent_from_backend(
        self,
        prompt: str,
        *,
        name: str | None = None,
        toolsets: list[str] | None = None,
        session_id: str | None = None,
        max_turns: int | None = None,
        agent_id: str = "",
    ) -> DurableCall[AgentResult[Any]]:
        """Route a prompt-only ctx.agent() call through the runtime's backend."""
        backend = self._runtime.agent_backend
        if backend is None:
            msg = (
                "ctx.agent('prompt') requires an agent_backend on the Runtime. "
                "Pass agent_backend=... to Runtime() or use ctx.agent(Agent(...), input) instead."
            )
            raise ConfigurationError(msg)

        identity = agent_id or "backend"
        label = name or f"agent:{identity}"

        if session_id is not None and not getattr(backend, "supports_history", False):
            raise ConfigurationError(
                f"{type(backend).__name__} does not support conversation history, so "
                f"session_id={session_id!r} would be ignored and every call would start "
                "from a blank conversation. Use BuiltInBackend, or drop session_id."
            )

        # Sessions are keyed by agent as well as conversation, so two agents
        # sharing a session id keep separate memories of it.
        memory_key = f"{identity}:{session_id}" if session_id else None

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            # Layer 3: resolve tools lazily from the registry, narrowed to what
            # this workflow declared it may use.
            tools = self._runtime.toolsets.resolve_tools(
                toolsets, grants=self._definition.grants
            )
            history = (
                await self._runtime.sessions.get(memory_key) if memory_key else []
            )
            result = await backend.run(
                prompt,
                tools=tools,
                history=history,
                agent_id=identity,
                max_turns=max_turns,
            )
            if memory_key and result.messages:
                await replace_history(
                    self._runtime.sessions, memory_key, result.messages
                )
            return result

        return DurableCall(
            self,
            kind=EntryKind.AGENT,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (prompt,)),
            input=prompt,
            output_type=AgentResult,
            retry=Retry(max_attempts=1),
            metadata={"agent_id": identity, "session_id": session_id},
        )

    # -- artifacts ----------------------------------------------------------------------

    def put_artifact(
        self,
        name: str,
        data: bytes | Attachment,
        *,
        mime: str = "application/octet-stream",
        **metadata: Any,
    ) -> DurableCall[ArtifactVersion]:
        """Publish a named artifact, returning the version it became.

        Journaled, so a replay returns the version the run originally produced
        instead of publishing a second one. Publishing byte-identical content
        twice is a no-op that resolves to the existing version.

        ``ctx.put_artifact("report.pdf", pdf_bytes)`` → ``report.pdf@1``, then
        ``@2`` when the bytes change.
        """
        payload = data
        label = f"artifact:put:{name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            service = self._runtime.require_artifacts()
            raw = payload.data or b"" if isinstance(payload, Attachment) else payload
            content_type = payload.mime if isinstance(payload, Attachment) else mime
            return await service.put(
                name, raw, mime=content_type, run_id=self.run_id, **metadata
            )

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (name,)),
            input={"name": name, "mime": mime},
            output_type=ArtifactVersion,
        )

    def get_artifact(self, name: str, version: int | None = None) -> DurableCall[bytes]:
        """Read a named artifact's content. ``version=None`` means latest.

        The resolved version is journaled, so a replay reads exactly what the
        original run read even if newer versions have been published since —
        without that, a replay is not a rehearsal of what happened but of what
        would happen now.
        """
        label = f"artifact:get:{name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            service = self._runtime.require_artifacts()
            resolved = await service.get(name, version)
            return await service.read(resolved.name, resolved.version)

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (name, version)),
            input={"name": name, "version": version},
            output_type=bytes,
        )

    def artifact_versions(self, name: str) -> DurableCall[list[ArtifactVersion]]:
        """List every published version of *name*, oldest first."""
        label = f"artifact:history:{name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            return await self._runtime.require_artifacts().history(name)

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (name,)),
            input={"name": name},
            output_type=list[ArtifactVersion],
        )

    # -- saga / compensation ------------------------------------------------------------

    async def compensate(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
    ) -> None:
        """Register a LIFO compensation handler.

        On workflow failure the compensation stack is executed in reverse
        order (most-recently-registered first).  Each handler runs
        best-effort: if it fails, the error is logged and the next
        handler still runs.
        """
        path = self._scope.allocate()
        recorded = self._journal.lookup(path, EntryKind.STEP, f"compensate:{fn.__name__}")
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            # Already registered on a prior attempt — rebuild the stack
            self._compensation_stack.append((fn, args))
            return

        self._compensation_stack.append((fn, args))
        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.STEP,
                name=f"compensate:{fn.__name__}",
                status=EntryStatus.COMPLETED,
                output=_encode_debug({"fn": fn.__name__, "args": args}),
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
                attempts=1,
            )
        )
        self._pending_flush += 1

    async def run_compensations(self) -> list[str]:
        """Execute the compensation stack in LIFO order (best-effort).

        Returns a list of handler names that failed.  Called by the
        engine when a workflow body raises.
        """
        failures: list[str] = []
        for fn, args in reversed(self._compensation_stack):
            try:
                await fn(*args)
            except Exception as exc:
                self.logger.error("compensation %s failed: %s", fn.__name__, exc)
                failures.append(fn.__name__)
        self._compensation_stack.clear()
        return failures

    # -- version gates ------------------------------------------------------------------

    def patched(self, name: str) -> bool:
        """Version gate for in-flight migration.

        Returns ``True`` if the named patch is active on this flow
        version.  Use to introduce backward-compatible behaviour changes
        that don't affect already-running executions.
        """
        return name in self._active_patches

    # -- forever-flow rotation ----------------------------------------------------------

    async def continue_as_new(self, seed: Any) -> NoReturn:
        """Rotate a forever-flow.

        Completes the current run and starts a fresh execution with
        ``seed`` as input, so the journal does not grow without bound.
        """
        path = self._scope.allocate()
        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.STEP,
                name="continue_as_new",
                status=EntryStatus.COMPLETED,
                output=_encode_durable(seed, what="continue_as_new seed"),
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
                attempts=1,
            )
        )
        await self._flush()
        raise ContinueAsNew(seed)

    # -- bookkeeping ------------------------------------------------------------------

    def set_metadata(self, **values: Any) -> None:
        """Attach queryable data to this execution, for later filtering in the store."""
        self._record.metadata.update(values)

    def tag(self, *tags: str) -> None:
        """Label this run for later filtering, e.g. ``ctx.tag("priority", "eu")``.

        Tags are queryable through ``runtime.list_runs(tags=[...])``. Use them for
        categories you will search by; use :meth:`set_metadata` for values.
        """
        for value in tags:
            if value not in self._record.tags:
                self._record.tags.append(value)

    def span(self, name: str, **attributes: Any) -> Span:
        """Open a tracing span around a region of orchestration code.

        Durable operations already get their own spans; this is for grouping
        several of them under one name in a trace viewer. Spans are observability
        only — nothing about them is journaled, so opening one cannot affect
        replay.
        """
        return self._tracer.start_span(name, attributes=attributes)

    async def checkpoint(self) -> None:
        """Force the journal to durable storage right now."""
        await self._flush()

    def raise_if_cancelled(self) -> None:
        """Abort now if cancellation has been requested.

        Cancellation is otherwise observed at the next durable operation. Call
        this inside a long stretch of pure computation so a cancelled run stops
        promptly rather than finishing work nobody wants.
        """
        if self._runtime.is_cancellation_requested(self.run_id):
            raise WorkflowCancelled(f"run {self.run_id} was cancelled")

    # -- internals --------------------------------------------------------------------

    async def _store_payload(self, encoded: Any) -> Any:
        """Offload an oversized journal payload to blob storage.

        Returns the value unchanged when no blob service is configured or the
        payload is small, so the common case stays a plain inline journal row.
        """
        blobs = self._runtime.blobs
        if blobs is None:
            return encoded
        raw = _json_bytes(encoded)
        if raw is None or not blobs.should_offload(raw):
            return encoded
        ref = await blobs.store(raw, "application/json")
        self.logger.debug("offloaded %d bytes to %s", len(raw), ref)
        return {BLOB_KEY: ref}

    async def _load_payload(self, stored: Any) -> Any:
        """Rehydrate a journal payload, following a blob reference if present."""
        if not _is_blob_marker(stored):
            return stored
        ref = stored[BLOB_KEY]
        blobs = self._runtime.blobs
        if blobs is None:
            raise ConfigurationError(
                f"journal entry references {ref} but this Runtime has no blob service. "
                "Pass the same blobs=BlobService(...) used when the run was recorded."
            )
        return json.loads(await blobs.load(ref))

    def _check_journal_size(self) -> None:
        """Warn, then fail, as a run's journal grows without bound.

        A forever-flow that never calls :meth:`continue_as_new` re-reads its whole
        journal on every attempt, so it degrades quadratically and silently. The
        warning fires once per run; the hard limit turns a slow death into a clear
        failure that names the fix.
        """
        runtime = self._runtime
        size = len(self._journal)

        if runtime.journal_max_entries and size >= runtime.journal_max_entries:
            raise BudgetExceeded(
                f"run {self.run_id} journaled {size} operations, over the limit of "
                f"{runtime.journal_max_entries}. A long-lived workflow should call "
                "ctx.continue_as_new(seed) to rotate into a fresh run, or raise "
                "journal_max_entries on the Runtime.",
                budget_type="journal_entries",
                limit=runtime.journal_max_entries,
                actual=size,
            )

        if not self._warned_journal_size and size >= runtime.journal_warn_entries:
            self._warned_journal_size = True
            self.logger.warning(
                "run %s has journaled %d operations; consider ctx.continue_as_new() "
                "to keep the journal bounded",
                self.run_id,
                size,
            )

    async def _maybe_flush(self, *, force: bool = False) -> None:
        self._pending_flush += 1
        self._check_journal_size()
        if force or self._pending_flush >= self._runtime.flush_every:
            await self._flush()

    async def _flush(self) -> None:
        self._pending_flush = 0
        await self._runtime.persist_journal(self._record, self._journal)
