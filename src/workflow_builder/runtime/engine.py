"""The durable execution engine.

The loop is deliberately small. Load the journal, re-enter the workflow body, and let the
journal short-circuit everything that already happened. The body either returns (done),
raises :class:`Suspend` (park until a timer or event), or raises (failed). Because every
side effect is journaled before it is observed, re-entering is always safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from workflow_builder.core.exceptions import (
    ConfigurationError,
    RegistryError,
    Suspend,
    WorkflowCancelled,
)
from workflow_builder.core.models import (
    ErrorInfo,
    Event,
    ExecutionRecord,
    ExecutionResult,
    ExecutionStatus,
    TriggerKind,
)
from workflow_builder.core.serde import decode, encode
from workflow_builder.core.types import Duration, to_seconds
from workflow_builder.observability.tracing import NoopTracer, Tracer
from workflow_builder.runtime.backend import DurabilityBackend, EmbeddedBackend
from workflow_builder.runtime.context import Context
from workflow_builder.runtime.journal import CompatibilityMode, Journal
from workflow_builder.runtime.workflow import WorkflowDefinition
from workflow_builder.state.memory import MemoryStore

logger = logging.getLogger("workflow.engine")


class Runtime:
    """Executes workflows against a store.

    A ``Runtime`` is cheap and holds no global state, so tests can spin up one per case
    and production can run one per process.
    """

    def __init__(
        self,
        *,
        store: Any | None = None,
        backend: DurabilityBackend | None = None,
        cache: Any | None = None,
        tracer: Tracer | None = None,
        credentials: Any | None = None,
        deps: Any = None,
        agent_backend: Any | None = None,
        inline_timer_threshold: Duration = 2.0,
        max_inline_wait: Duration = 0.0,
        flush_every: int = 1,
        compatibility: CompatibilityMode = CompatibilityMode.STRICT,
        strict_determinism: bool = False,
    ) -> None:
        if backend is not None:
            self.backend = backend
        else:
            raw_store = store or MemoryStore()
            self.backend = EmbeddedBackend(raw_store)
        # Convenience alias — existing code and stores use self.store.
        if isinstance(self.backend, EmbeddedBackend):
            self.store = self.backend._store
        else:
            self.store = store or MemoryStore()
        self.cache = cache if cache is not None else self.store
        self.tracer: Tracer = tracer or NoopTracer()
        self.credentials = credentials
        self.deps = deps
        self.agent_backend = agent_backend
        from workflow_builder.agents.tool_registry import ToolsetRegistry
        self.toolsets = ToolsetRegistry()
        self.compatibility = compatibility
        self.strict_determinism = strict_determinism

        self.inline_timer_threshold = to_seconds(inline_timer_threshold)
        """Sleeps shorter than this stay in memory instead of parking the run."""
        self.max_inline_wait = to_seconds(max_inline_wait)
        """How long ``run()`` will block on a parked run before returning SUSPENDED."""
        self.flush_every = max(1, flush_every)

        self._workflows: dict[str, WorkflowDefinition[Any, Any, Any]] = {}
        self._limiters: dict[str, asyncio.Semaphore] = {}
        self._workflow_limiters: dict[str, asyncio.Semaphore] = {}
        self._completion: dict[str, asyncio.Event] = {}
        self._event_waiters: dict[tuple[str, str], asyncio.Event] = {}
        self._cancelled: set[str] = set()
        self._driving: set[str] = set()
        self._background: set[asyncio.Task[Any]] = set()
        self._scheduler_task: asyncio.Task[None] | None = None

    # -- registration -----------------------------------------------------------------

    def register(
        self, definition: WorkflowDefinition[Any, Any, Any]
    ) -> WorkflowDefinition[Any, Any, Any]:
        existing = self._workflows.get(definition.name)
        if existing is not None and existing is not definition:
            raise ConfigurationError(
                f"a different workflow named '{definition.name}' is registered"
            )
        self._workflows[definition.name] = definition
        return definition

    def register_all(self, definitions: Sequence[WorkflowDefinition[Any, Any, Any]]) -> None:
        for definition in definitions:
            self.register(definition)

    @property
    def workflows(self) -> dict[str, WorkflowDefinition[Any, Any, Any]]:
        return dict(self._workflows)

    def resolve_workflow(
        self, target: WorkflowDefinition[Any, Any, Any] | str
    ) -> WorkflowDefinition[Any, Any, Any]:
        if isinstance(target, WorkflowDefinition):
            return self.register(target)
        found = self._workflows.get(target)
        if found is None:
            known = ", ".join(sorted(self._workflows)) or "none"
            raise RegistryError(f"no workflow named '{target}' is registered (known: {known})")
        return found

    # -- starting work ----------------------------------------------------------------

    async def run(
        self,
        target: WorkflowDefinition[Any, Any, Any] | str,
        input: Any = None,
        *,
        deps: Any = None,
        run_id: str | None = None,
        trigger: TriggerKind = TriggerKind.MANUAL,
        idempotency_key: str | None = None,
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        tags: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Start a workflow and drive it until it finishes or parks."""
        definition = self.resolve_workflow(target)

        if idempotency_key:
            existing = await self.store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                logger.info(
                    "idempotency hit for %s, returning run %s",
                    idempotency_key,
                    existing.run_id,
                )
                return await self._result_for(existing)

        record = ExecutionRecord(
            workflow=definition.name,
            workflow_version=definition.version,
            status=ExecutionStatus.PENDING,
            trigger=trigger,
            input=encode(input),
            parent_run_id=parent_run_id,
            root_run_id=root_run_id or parent_run_id,
            idempotency_key=idempotency_key,
            created_at=datetime.now(UTC),
            tags=list(tags),
            metadata=dict(metadata or {}),
        )
        if run_id:
            record.run_id = run_id
        await self.store.create_execution(record)
        return await self._drive(record.run_id, deps=deps)

    async def submit(
        self,
        target: WorkflowDefinition[Any, Any, Any] | str,
        input: Any = None,
        **kwargs: Any,
    ) -> str:
        """Start a workflow in the background and return its run id immediately."""
        definition = self.resolve_workflow(target)
        record = ExecutionRecord(
            workflow=definition.name,
            workflow_version=definition.version,
            status=ExecutionStatus.PENDING,
            trigger=kwargs.pop("trigger", TriggerKind.MANUAL),
            input=encode(input),
            parent_run_id=kwargs.pop("parent_run_id", None),
            idempotency_key=kwargs.pop("idempotency_key", None),
            created_at=datetime.now(UTC),
        )
        await self.store.create_execution(record)
        deps = kwargs.pop("deps", None)
        self._spawn(self._drive(record.run_id, deps=deps))
        return record.run_id

    async def resume(self, run_id: str, *, deps: Any = None) -> ExecutionResult:
        """Continue a parked run. Safe to call redundantly."""
        return await self._drive(run_id, deps=deps)

    async def retry(
        self,
        run_id: str,
        *,
        use_current_code: bool = True,
        deps: Any = None,
    ) -> ExecutionResult:
        """Re-run a failed execution, reusing everything that already succeeded.

        This is the operational feature that makes durable execution worth the trouble: a
        run that died at step 14 of 20 resumes at step 14, against the code as it exists
        now, with the first thirteen results intact.
        """
        record = await self._require(run_id)
        journal = Journal(await self.store.load_journal(run_id))
        failures = journal.failed_entries()
        if failures:
            first_failed = failures[0].path
            await self.store.truncate_journal(run_id, first_failed)
            logger.info("retrying %s from %s (%s)", run_id, first_failed, failures[0].name)

        record.status = ExecutionStatus.PENDING
        record.error = None
        record.finished_at = None
        record.wake_at = None
        record.awaiting_event = None
        await self.store.update_execution(record)

        previous = self.compatibility
        if use_current_code:
            self.compatibility = CompatibilityMode.RESUME_FROM_DIVERGENCE
        try:
            return await self._drive(run_id, deps=deps)
        finally:
            self.compatibility = previous

    async def replay(self, run_id: str, *, deps: Any = None) -> ExecutionResult:
        """Re-execute a run from its recorded inputs without repeating side effects.

        Every journaled result is served from the journal, so this is a free, offline
        rehearsal of the orchestration logic — the code-first answer to "what would this
        have done?".
        """
        source = await self._require(run_id)
        clone = source.model_copy(deep=True)
        clone.run_id = f"{run_id}:replay"
        clone.replay_of = run_id
        clone.status = ExecutionStatus.PENDING
        clone.trigger = TriggerKind.REPLAY
        clone.error = None
        clone.finished_at = None
        await self.store.create_execution(clone)
        await self.store.save_journal(clone.run_id, await self.store.load_journal(run_id))
        return await self._drive(clone.run_id, deps=deps)

    async def cancel(self, run_id: str, *, reason: str = "cancelled by request") -> None:
        """Request cancellation. Takes effect at the next durable operation."""
        self._cancelled.add(run_id)
        record = await self.store.get_execution(run_id)
        if record is not None and not record.status.is_terminal:
            record.status = ExecutionStatus.CANCELLED
            record.error = ErrorInfo(type="WorkflowCancelled", message=reason, retryable=False)
            record.finished_at = datetime.now(UTC)
            await self.store.update_execution(record)
            self._signal_completion(run_id)

    def is_cancellation_requested(self, run_id: str) -> bool:
        return run_id in self._cancelled

    # -- events -----------------------------------------------------------------------

    async def send_event(self, run_id: str | None, name: str, payload: Any = None) -> None:
        """Deliver an event. Resumes the target run if it is parked waiting for it."""
        await self.store.enqueue_event(Event(name=name, payload=encode(payload), run_id=run_id))

        waiter = self._event_waiters.get((run_id or "", name))
        if waiter is not None:
            waiter.set()

        targets = [run_id] if run_id else await self.store.runs_awaiting_event(name)
        for target in targets:
            if target is None:
                continue
            record = await self.store.get_execution(target)
            if (
                record is not None
                and record.status is ExecutionStatus.SUSPENDED
                and record.awaiting_event == name
                and target not in self._driving
            ):
                self._spawn(self._drive(target))

    async def take_event(self, run_id: str, name: str) -> Event | None:
        event = await self.store.take_event(run_id, name)
        if event is not None and event.payload is not None:
            return event
        return event

    async def approve(self, run_id: str, subject: str, *, approved: bool = True) -> None:
        """Resolve a pending human approval."""
        await self.send_event(run_id, f"approval:{subject}", {"approved": approved})

    # -- queries ----------------------------------------------------------------------

    async def get(self, run_id: str) -> ExecutionRecord | None:
        return await self.store.get_execution(run_id)

    async def result(self, run_id: str) -> ExecutionResult:
        return await self._result_for(await self._require(run_id))

    async def list_runs(self, **filters: Any) -> list[ExecutionRecord]:
        return await self.store.list_executions(**filters)

    async def history(self, run_id: str) -> list[Any]:
        journal = Journal(await self.store.load_journal(run_id))
        return journal.records()

    async def wait(self, run_id: str, *, timeout: Duration | None = None) -> ExecutionResult:
        """Block until a run reaches a terminal state."""
        record = await self.store.get_execution(run_id)
        if record is not None and record.status.is_terminal:
            return await self._result_for(record)

        event = self._completion.setdefault(run_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), to_seconds(timeout) if timeout else None)
        return await self.result(run_id)

    # -- timers -----------------------------------------------------------------------

    async def tick(self, now: datetime | None = None, *, limit: int = 100) -> list[str]:
        """Resume every run whose timer has expired. Call from a scheduler loop."""
        due = await self.store.due_runs(now or datetime.now(UTC), limit=limit)
        resumed: list[str] = []
        for run_id in due:
            if run_id in self._driving:
                continue
            resumed.append(run_id)
            self._spawn(self._drive(run_id))
        return resumed

    async def start_scheduler(self, *, interval: Duration = 1.0) -> None:
        """Run the timer scanner in the background until :meth:`shutdown`."""
        if self._scheduler_task is not None:
            return

        async def loop() -> None:
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("scheduler tick failed")
                await asyncio.sleep(to_seconds(interval))

        self._scheduler_task = asyncio.create_task(loop())

    async def shutdown(self) -> None:
        """Stop the scheduler and let in-flight background drives settle."""
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
            self._scheduler_task = None
        pending = [task for task in self._background if not task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background.clear()

    # -- concurrency ------------------------------------------------------------------

    def limiter_for(self, step_name: str, concurrency_key: str | None) -> asyncio.Semaphore | None:
        """Semaphore shared by every step declaring the same ``concurrency_key``."""
        if not concurrency_key:
            return None
        limit_key, _, limit_text = concurrency_key.partition(":")
        limit = int(limit_text) if limit_text.isdigit() else 1
        if limit_key not in self._limiters:
            self._limiters[limit_key] = asyncio.Semaphore(limit)
        return self._limiters[limit_key]

    # -- persistence ------------------------------------------------------------------

    async def persist_journal(self, record: ExecutionRecord, journal: Journal) -> None:
        dirty = journal.drain_dirty()
        if dirty:
            await self.store.save_journal(record.run_id, dirty)

    # -- the core loop ----------------------------------------------------------------

    async def _drive(self, run_id: str, *, deps: Any = None) -> ExecutionResult:
        if run_id in self._driving:
            return await self.wait(run_id, timeout=self.max_inline_wait or 30)
        self._driving.add(run_id)
        try:
            return await self._drive_inner(run_id, deps=deps)
        finally:
            self._driving.discard(run_id)

    async def _drive_inner(self, run_id: str, *, deps: Any) -> ExecutionResult:
        record = await self._require(run_id)
        definition = self.resolve_workflow(record.workflow)

        while True:
            if record.status.is_terminal:
                return await self._result_for(record)
            if run_id in self._cancelled:
                return await self._finish_cancelled(record)

            journal = Journal(
                await self.store.load_journal(run_id), compatibility=self.compatibility
            )
            record.status = ExecutionStatus.RUNNING
            record.attempt += 1
            record.started_at = record.started_at or datetime.now(UTC)
            record.wake_at = None
            record.awaiting_event = None
            await self.store.update_execution(record)

            ctx = Context(
                runtime=self,
                record=record,
                journal=journal,
                definition=definition,
                deps=deps if deps is not None else self.deps,
            )

            span = self.tracer.start_span(
                f"workflow.{definition.name}",
                attributes={"run_id": run_id, "attempt": record.attempt},
            )
            try:
                payload = decode(record.input, definition.input_type)
                coro = definition.invoke(ctx, payload)
                if definition.timeout is not None:
                    output = await asyncio.wait_for(coro, to_seconds(definition.timeout))
                else:
                    output = await coro
            except Suspend as suspension:
                await self.persist_journal(record, journal)
                should_continue = await self._park(record, suspension, journal)
                if should_continue:
                    continue
                span.set_status("suspended")
                span.end()
                return await self._result_for(record, journal)
            except WorkflowCancelled:
                span.set_status("cancelled")
                span.end()
                await self.persist_journal(record, journal)
                return await self._finish_cancelled(record, journal)
            except Exception as error:
                span.record_exception(error)
                span.end()
                await self.persist_journal(record, journal)
                return await self._finish_failed(record, journal, error, definition)
            else:
                span.set_status("ok")
                span.end()
                await self.persist_journal(record, journal)
                return await self._finish_completed(record, journal, output)

    async def _park(self, record: ExecutionRecord, suspension: Suspend, journal: Journal) -> bool:
        """Persist a suspension. Returns True if we should immediately re-enter the body."""
        record.status = ExecutionStatus.SUSPENDED
        record.wake_at = suspension.wake_at
        record.awaiting_event = suspension.awaiting_event
        record.usage = journal.total_usage()
        await self.store.update_execution(record)
        logger.debug("run %s suspended: %s", record.run_id, suspension.reason)

        if suspension.awaiting_event:
            waiter = self._event_waiters.setdefault(
                (record.run_id, suspension.awaiting_event), asyncio.Event()
            )
            waiter.clear()
            budget = self.max_inline_wait
            if suspension.wake_at is not None:
                remaining = (suspension.wake_at - datetime.now(UTC)).total_seconds()
                budget = max(budget, min(remaining, self.max_inline_wait))
            if budget <= 0:
                return False
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(waiter.wait(), budget)
            return True

        if suspension.wake_at is not None:
            remaining = (suspension.wake_at - datetime.now(UTC)).total_seconds()
            if remaining <= self.max_inline_wait:
                await asyncio.sleep(max(0.0, remaining))
                return True
        return False

    async def _finish_completed(
        self, record: ExecutionRecord, journal: Journal, output: Any
    ) -> ExecutionResult:
        record.status = ExecutionStatus.COMPLETED
        record.output = encode(output)
        record.finished_at = datetime.now(UTC)
        record.usage = journal.total_usage()
        await self.store.update_execution(record)
        self._signal_completion(record.run_id)
        return await self._result_for(record, journal, raw_output=output)

    async def _finish_failed(
        self,
        record: ExecutionRecord,
        journal: Journal,
        error: BaseException,
        definition: WorkflowDefinition[Any, Any, Any],
    ) -> ExecutionResult:
        record.status = ExecutionStatus.FAILED
        record.error = ErrorInfo.from_exception(error)
        record.finished_at = datetime.now(UTC)
        record.usage = journal.total_usage()
        await self.store.update_execution(record)
        self._signal_completion(record.run_id)
        logger.warning("run %s failed: %s", record.run_id, error)

        await self._dispatch_failure_handlers(record, definition)
        return await self._result_for(record, journal)

    async def _finish_cancelled(
        self, record: ExecutionRecord, journal: Journal | None = None
    ) -> ExecutionResult:
        record.status = ExecutionStatus.CANCELLED
        record.finished_at = datetime.now(UTC)
        await self.store.update_execution(record)
        self._signal_completion(record.run_id)
        return await self._result_for(record, journal)

    async def _dispatch_failure_handlers(
        self, record: ExecutionRecord, definition: WorkflowDefinition[Any, Any, Any]
    ) -> None:
        """Invoke the workflow's own handler plus any registered ``OnFailure`` workflows."""
        from workflow_builder.triggers.specs import OnFailure

        envelope = {
            "execution": {
                "run_id": record.run_id,
                "attempt": record.attempt,
                "error": record.error.model_dump() if record.error else None,
                "trigger": record.trigger.value,
                "input": record.input,
            },
            "workflow": {"name": record.workflow, "version": record.workflow_version},
        }

        handlers: list[str] = []
        if definition.on_failure:
            handlers.append(definition.on_failure)
        for candidate in self._workflows.values():
            if candidate.name == record.workflow:
                continue
            for spec in candidate.triggers:
                if isinstance(spec, OnFailure) and spec.handles(record.workflow):
                    handlers.append(candidate.name)

        for handler in dict.fromkeys(handlers):
            try:
                self._spawn(
                    self.run(handler, envelope, trigger=TriggerKind.ERROR_HANDLER)  # type: ignore[arg-type]
                )
            except RegistryError:
                logger.error("failure handler '%s' is not registered", handler)

    # -- helpers ----------------------------------------------------------------------

    async def _require(self, run_id: str) -> ExecutionRecord:
        record = await self.store.get_execution(run_id)
        if record is None:
            raise RegistryError(f"no execution with id '{run_id}'")
        return record

    async def _result_for(
        self,
        record: ExecutionRecord,
        journal: Journal | None = None,
        *,
        raw_output: Any = None,
    ) -> ExecutionResult:
        entries = journal or Journal(await self.store.load_journal(record.run_id))
        return ExecutionResult(
            run_id=record.run_id,
            workflow=record.workflow,
            status=record.status,
            output=raw_output if raw_output is not None else record.output,
            error=record.error,
            steps=entries.records(),
            usage=record.usage,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )


    def _signal_completion(self, run_id: str) -> None:
        event = self._completion.get(run_id)
        if event is not None:
            event.set()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task
