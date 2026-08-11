"""The durable execution context.

``Context`` is the only legal doorway from deterministic orchestration code to the
non-deterministic world. Every method on it either journals its result or is a pure
function of already-journaled state, which is what lets an execution be replayed after a
crash, a deploy, or a three-week wait for a human to click "approve".
"""

from __future__ import annotations

import asyncio
import logging
import random as _random
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeVar

from workflow_builder.core.exceptions import (
    ConfigurationError,
    ContinueAsNew,
    ControlSignal,
    RetriesExhausted,
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
from workflow_builder.runtime.journal import (
    EntryKind,
    EntryStatus,
    Journal,
    JournalEntry,
    Scope,
)
from workflow_builder.steps.context import StepContext
from workflow_builder.steps.definition import StepDefinition

if TYPE_CHECKING:
    from workflow_builder.agents.agent import Agent
    from workflow_builder.agents.result import AgentResult
    from workflow_builder.core.models import ExecutionRecord
    from workflow_builder.observability.tracing import Span
    from workflow_builder.runtime.engine import Runtime
    from workflow_builder.runtime.workflow import WorkflowDefinition

T = TypeVar("T")


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

    def __await__(self) -> Any:
        return self._task().__await__()

    def _task(self) -> asyncio.Future[Any]:
        """Memoize execution so awaiting the same call twice does not run it twice."""
        if self._future is None:
            self._future = asyncio.ensure_future(self._resolve())
        return self._future

    async def _resolve(self) -> Any:
        ctx = self._ctx
        journal = ctx._journal

        recorded = journal.lookup(self.path, self.kind, self.name)
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            return decode(recorded.output, self._output_type)
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
            input=_safe_encode(self._input),
            status=EntryStatus.PENDING,
            started_at=_utcnow(),
            metadata={k: v for k, v in self._metadata.items() if v is not None},
        )
        journal.put(entry)

        span = ctx._tracer.start_span(
            f"{self.kind.value}.{self.name}",
            attributes={"workflow": ctx.workflow, "run_id": ctx.run_id, "path": self.path},
        )
        attempt = 0
        last_error: BaseException | None = None
        try:
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
                    result = await self._invoke(attempt, step_ctx)
                except ControlSignal:
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
                    await asyncio.sleep(self._retry.delay_for(attempt, rng=ctx._backoff_rng))
                else:
                    entry.status = EntryStatus.COMPLETED
                    entry.output = _safe_encode(result)
                    entry.finished_at = _utcnow()
                    if isinstance(usage := getattr(result, "usage", None), Usage):
                        entry.usage = usage
                    journal.put(entry)
                    span.set_status("ok")
                    await ctx._maybe_flush()
                    return result

            assert last_error is not None
            entry.status = EntryStatus.FAILED
            entry.error = ErrorInfo.from_exception(last_error, step_name=self.name)
            entry.finished_at = _utcnow()
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
        finally:
            span.end()

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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_encode(value: Any) -> Any:
    try:
        return encode(value)
    except Exception:
        return {"__unserializable__": type(value).__name__}


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
        self._backoff_rng = _random.Random(record.run_id)
        self._pending_flush = 0
        self._compensation_stack: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...]]] = []
        self._active_patches: frozenset[str] = frozenset()

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

        value = _safe_encode(produce())
        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.SIDE_EFFECT,
                name=name,
                status=EntryStatus.COMPLETED,
                output=value,
                started_at=_utcnow(),
                finished_at=_utcnow(),
                attempts=1,
            )
        )
        self._pending_flush += 1
        return value

    def now(self) -> datetime:
        """The current UTC time, frozen into the journal so replays agree."""
        return datetime.fromisoformat(self._side_effect("now", lambda: _utcnow().isoformat()))

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
            started_at=_utcnow(),
        )
        entry.wake_at = when
        self._journal.put(entry)

        remaining = (when - _utcnow()).total_seconds()
        if remaining <= 0:
            entry.status = EntryStatus.COMPLETED
            entry.finished_at = _utcnow()
            self._journal.put(entry)
            return

        if remaining <= self._runtime.inline_timer_threshold:
            # Short waits are cheaper to hold in memory than to park and rehydrate.
            await self._flush()
            await asyncio.sleep(remaining)
            entry.status = EntryStatus.COMPLETED
            entry.finished_at = _utcnow()
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
            payload = _safe_encode(delivered.payload)
            self._journal.put(
                JournalEntry(
                    path=path,
                    kind=EntryKind.EVENT,
                    name=name,
                    status=EntryStatus.COMPLETED,
                    output=payload,
                    started_at=_utcnow(),
                    finished_at=_utcnow(),
                )
            )
            await self._maybe_flush()
            return decode(payload, output_type)

        if deadline is not None and deadline <= _utcnow():
            self._journal.put(
                JournalEntry(
                    path=path,
                    kind=EntryKind.EVENT,
                    name=name,
                    status=EntryStatus.COMPLETED,
                    output=_safe_encode(default),
                    metadata={"timed_out": True},
                    started_at=_utcnow(),
                    finished_at=_utcnow(),
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
                started_at=_utcnow(),
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

    def agent(
        self,
        agent_or_prompt: Agent[Any, T] | str,
        input: Any = None,
        *,
        name: str | None = None,
        max_turns: int | None = None,
        session_id: str | None = None,
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
        """
        if isinstance(agent_or_prompt, str) and input is None:
            return self._agent_from_backend(
                agent_or_prompt, name=name, toolsets=toolsets,
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
            retry=Retry(max_attempts=1),
        )

    def _agent_from_backend(
        self,
        prompt: str,
        *,
        name: str | None = None,
        toolsets: list[str] | None = None,
    ) -> DurableCall[AgentResult[Any]]:
        """Route a prompt-only ctx.agent() call through the runtime's backend."""
        backend = self._runtime.agent_backend
        if backend is None:
            msg = (
                "ctx.agent('prompt') requires an agent_backend on the Runtime. "
                "Pass agent_backend=... to Runtime() or use ctx.agent(Agent(...), input) instead."
            )
            raise ConfigurationError(msg)

        label = name or "agent:backend"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            # Layer 3: resolve tools lazily from the registry
            tools = self._runtime.toolsets.resolve_tools(toolsets)
            return await backend.run(prompt, tools=tools)

        return DurableCall(
            self,
            kind=EntryKind.AGENT,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (prompt,)),
            input=prompt,
            retry=Retry(max_attempts=1),
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
                output=_safe_encode({"fn": fn.__name__, "args": args}),
                started_at=_utcnow(),
                finished_at=_utcnow(),
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
                output=_safe_encode({"rotation_seed": seed}),
                started_at=_utcnow(),
                finished_at=_utcnow(),
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
        for value in tags:
            if value not in self._record.tags:
                self._record.tags.append(value)

    def span(self, name: str, **attributes: Any) -> Span:
        return self._tracer.start_span(name, attributes=attributes)

    async def checkpoint(self) -> None:
        """Force the journal to durable storage right now."""
        await self._flush()

    def raise_if_cancelled(self) -> None:
        if self._runtime.is_cancellation_requested(self.run_id):
            raise WorkflowCancelled(f"run {self.run_id} was cancelled")

    # -- internals --------------------------------------------------------------------

    async def _maybe_flush(self, *, force: bool = False) -> None:
        self._pending_flush += 1
        if force or self._pending_flush >= self._runtime.flush_every:
            await self._flush()

    async def _flush(self) -> None:
        self._pending_flush = 0
        await self._runtime.persist_journal(self._record, self._journal)
