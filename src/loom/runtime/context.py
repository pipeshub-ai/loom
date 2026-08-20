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
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeVar, overload

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from loom.agents.memory import replace_history
from loom.agents.result import AgentResult
from loom.blobs.artifact import ArtifactVersion
from loom.blobs.attachment import Attachment
from loom.blobs.blob import BlobNotFoundError
from loom.connectors.credentials import credential_store_scope
from loom.core.exceptions import (
    AuthExpired,
    BudgetExceeded,
    ConfigurationError,
    ContinueAsNew,
    ContractChanged,
    ControlSignal,
    DataUnavailable,
    RetriesExhausted,
    SerializationError,
    StepError,
    Suspend,
    TimeoutExceeded,
    WorkflowCancelled,
)
from loom.core.ids import callable_name
from loom.core.ids import fingerprint as make_fingerprint
from loom.core.models import ErrorInfo, Usage
from loom.core.redaction import (
    DEFAULT_REDACT_KEYS,
    redact,
    redact_call_input,
    strip_secret_values,
)
from loom.core.retry import Failure, OnError, Retry
from loom.core.serde import decode, drift_of, encode
from loom.core.types import DepsT, Duration, to_seconds
from loom.nodes.errors import NodeContractError
from loom.runtime.determinism import step_scope
from loom.runtime.effects import EffectCall, EffectDenied
from loom.runtime.journal import (
    EntryKind,
    EntryStatus,
    Journal,
    JournalEntry,
    Scope,
)
from loom.security.authority import Authority
from loom.steps.context import StepContext
from loom.steps.definition import StepDefinition
from loom.toolsets.effects import resolve_effect
from loom.toolsets.manifest import EffectClass

if TYPE_CHECKING:
    from loom.agents.agent import Agent
    from loom.core.models import ExecutionRecord
    from loom.observability.tracing import Span
    from loom.runtime.engine import Runtime
    from loom.runtime.workflow import WorkflowDefinition

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

        recorded = journal.lookup(
            self.path, self.kind, self.name, fingerprint=self._fingerprint
        )
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            self._check_contract(recorded)
            value = decode(
                await ctx._load_payload(
                    recorded.output, where=f"{self.kind.value} '{self.name}' (path {self.path})"
                ),
                self._output_type,
            )
            self._note_drift(recorded, value)
            return value
        if (
            recorded is not None
            and recorded.status is EntryStatus.EXHAUSTED
            and journal.resume_exhausted
        ):
            # Recorded, but not an answer. The step spent its own retry budget;
            # whether the *run* is finished was never its call. Falling through
            # re-executes it with the other entries still served from the
            # journal, which is what "resume at step 9" has to mean for a
            # transient outage — where retry() would prune the history instead.
            recorded = None
        if recorded is not None and recorded.status in (
            EntryStatus.FAILED,
            EntryStatus.EXHAUSTED,
        ):
            # Replay must surface the failure the workflow originally saw. To re-run the
            # step against fixed code use runtime.retry(), which prunes failed entries.
            return self._handle_failure(
                self._recorded_failure(recorded), attempts=recorded.attempts
            )

        # Nothing recorded, so this call is about to *happen*. That makes it
        # the boundary a cancellation takes effect at — after every already
        # answered call is served from the journal, and before a new side
        # effect is performed. Raising here rather than only at the top of the
        # body is what lets a cancel land mid-body: the exception unwinds
        # through the workflow, the engine catches it, and the compensation
        # stack runs.
        ctx.raise_if_cancelled()
        ctx.park_if_paused()

        entry = JournalEntry(
            path=self.path,
            kind=self.kind,
            name=self.name,
            fingerprint=self._fingerprint,
            input=_encode_debug(self._input, ctx._runtime.redact_keys),
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
        if outcome.needs:
            # `ErrorInfo` carries a type and a message and nothing else, so the
            # grant that would have allowed this is recorded beside it — that is
            # the half a replay needs to rebuild a denial worth acting on.
            entry.metadata["denied_needs"] = outcome.needs
        entry.finished_at = self._clock.now()
        journal.put(entry)
        await ctx._maybe_flush(force=True)
        return self._handle_failure(refusal, attempts=entry.attempts)

    def _note_drift(self, recorded: JournalEntry, value: Any) -> None:
        """Report a journaled value that no longer fits its declared type.

        ``decode`` hands the raw payload back rather than failing, so an
        in-flight run survives a refactor. Left silent, the workflow then gets a
        dict where it declared a model and fails at an attribute access several
        lines on — a symptom that points at the wrong place. This puts the
        cause on the entry and in the log, once.
        """

        reason = drift_of(value, self._output_type)
        if reason is None:
            return
        recorded.metadata["contract_drift"] = reason
        self._ctx._journal.mark_dirty(recorded.path)
        self._ctx.logger.warning(
            "replay contract drift at %s (%s '%s'): the journaled value no "
            "longer decodes into %s, so the raw payload was used. %s",
            recorded.path,
            recorded.kind.value,
            recorded.name,
            reason.split(":")[0],
            "Pin the previous version to replay this run, or use runtime.retry() "
            "to re-execute against current code.",
        )

    def _check_contract(self, recorded: JournalEntry) -> None:
        """Refuse to replay a journaled result through a changed contract.

        A call that declares a ``contract`` in its metadata is asserting which
        input/output shape produced the stored value. If the installed code now
        declares a different one, decoding the old payload into the new model
        would let an upgrade quietly change what an old run replays to — the run
        would appear to have done something it never did.

        Only checked when *both* sides declare one, so entries journaled before
        this existed replay unchanged.
        """
        declared = self._metadata.get("contract")
        stored = (recorded.metadata or {}).get("contract")
        if not declared or not stored or declared == stored:
            return
        target = self._metadata.get("node_id") or self.name
        raise ContractChanged(
            f"{target} was journaled under contract {stored} and the installed "
            f"version declares {declared}. Replaying the stored value through a "
            "changed input/output shape would make this run appear to have done "
            "something it did not. Pin the previous version to replay this run, "
            "or use runtime.retry() to re-execute against current code."
        )

    def _recorded_failure(self, recorded: JournalEntry) -> BaseException:
        """The exception a replay should raise for a failure already journaled.

        Reproducing the *message* is not enough. A workflow branches on the
        exception **type** — ``except EffectDenied`` is how it tells "policy
        refused this" from "this broke" — so rebuilding every recorded failure
        as a generic :class:`StepError` sends a replay down a different branch
        from the run it is supposed to be rehearsing. Journaling the denial was
        meant to prevent exactly that divergence; losing the type reintroduced
        it one layer down.

        **Only failures the engine itself produced are rebuilt.** The engine
        knows their constructors. A ``ValueError`` from inside a step is left as
        a ``StepError`` carrying its message, because rebuilding an arbitrary
        exception from a name and a string means guessing at a signature — and
        the class may not even be importable in the process doing the replay.
        That is a real remaining gap, and a narrower one than pretending to
        close it would be.
        """
        message = recorded.error.message if recorded.error else "step failed"
        if recorded.error is not None and recorded.error.type == "EffectDenied":
            return EffectDenied(
                message,
                # Rebuilt from the entry rather than carried, so it describes
                # the call the denial was actually about.
                call=EffectCall(
                    kind=_EFFECT_KINDS.get(self.kind, self.kind.value),
                    target=self.name,
                    run_id=self._ctx.run_id,
                    path=self.path,
                ),
                # The actionable half — "add this grant and it works". A
                # replayed denial without it is a worse object than the one the
                # run saw.
                needs=(recorded.metadata or {}).get("denied_needs", ""),
            )
        if recorded.status is EntryStatus.EXHAUSTED and recorded.attempts > 1:
            # The run raised `RetriesExhausted` only when it actually retried —
            # ``attempt > 1`` in the loop above — so the same condition rebuilds
            # it, read from the attempt count the journal already carries.
            # ``EntryStatus.EXHAUSTED`` is the purpose-built signal here: the
            # journal records the *original* error's type, because that is the
            # useful one to keep, so the status is what says a retry budget was
            # spent rather than the type.
            #
            # Safe to widen an existing replay from `StepError` to this because
            # `RetriesExhausted` **is** a `StepError` — anything already
            # catching the base class keeps catching it.
            return RetriesExhausted(
                f"step '{self.name}' failed after {recorded.attempts} "
                f"attempts: {message}",
                step_name=self.name,
                attempts=recorded.attempts,
            )
        return StepError(message, step_name=self.name, attempts=recorded.attempts)

    def _effect_call(self, entry: JournalEntry, span: Span) -> EffectCall:
        """Describe this operation for the broker, and how to carry it out."""

        async def perform() -> Any:
            return await self._attempt_loop(entry, span)

        return EffectCall(
            # A node journals as a step — nodes add packaging, not a second
            # durability mechanism — but it is not a step to a *policy*, which
            # has a catalogue of node ids to decide against and no manifest for
            # `control` or `human`. The journal keeps its kind; the broker is
            # told what this really is.
            kind=self._metadata.get("effect_kind")
            or _EFFECT_KINDS.get(self.kind, self.kind.value),
            target=self._metadata.get("effect_target") or self.name,
            arguments=_effect_arguments(self._input),
            effect=resolve_effect(
                self._metadata.get("effect_class") or EffectClass.WRITE,
                self._metadata.get("effect_by") or {},
                _effect_arguments(self._input),
            ),
            open_world=self._metadata.get("open_world", True),
            reversible=self._metadata.get("reversible", False),
            access_control=self._metadata.get("access_control", False),
            run_id=self._ctx.run_id,
            path=self.path,
            perform=perform,
            # Local-only, like `perform`, and absent from `describe()`. Lets a
            # hook journal its own work beneath this call's path.
            context=self._ctx,
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
                credentials=ctx._credentials,
                span=span,
            )
            try:
                # Bound for the duration of this attempt so a toolset's
                # process-wide client singleton (get_default_client(),
                # get_default_auth()) can resolve this run's CredentialStore
                # with no `ctx` parameter of its own — see
                # connectors/credentials.py::current_credential_store().
                with credential_store_scope(ctx._credentials):
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
        # EXHAUSTED, not FAILED: this step is done trying, the run is not
        # necessarily done. The engine promotes it when the run goes terminal
        # with nobody having claimed it.
        entry.status = EntryStatus.EXHAUSTED
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


def _encode_debug(value: Any, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> Any:
    """Encode a value recorded only for observability.

    Step inputs and signal payloads are written for humans reading a trace; they
    are never replayed into the workflow. Degrading here loses a debugging aid,
    not correctness, so a stubborn value is marked rather than fatal.

    Three passes, in this order for a reason. Secret-typed values go first,
    because :class:`~loom.core.secret.Secret` deliberately refuses to encode —
    leaving one in place made the *whole* input degrade to
    ``{"__unserializable__": "dict"}``, which is safe and tells a reader nothing
    about the arguments beside it. Encoding turns models into mappings. Only
    then does the name denylist run, so a nested model's ``api_key`` field is
    reached by the same rule as a plain dict key.
    """
    try:
        return redact(encode(strip_secret_values(value)), keys)
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
        found = await self._dispatch(
            "get", key, EffectClass.READ, lambda: self._store.get(self._workflow, key)
        )
        return default if found is None else found

    async def set(self, key: str, value: Any) -> None:
        await self._dispatch(
            "set", key, EffectClass.WRITE,
            lambda: self._store.set(self._workflow, key, value),
        )

    async def delete(self, key: str) -> None:
        await self._dispatch(
            "delete", key, EffectClass.DESTRUCTIVE,
            lambda: self._store.delete(self._workflow, key),
        )

    async def keys(self) -> list[str]:
        found = await self._dispatch(
            "keys", "", EffectClass.READ, lambda: self._store.keys(self._workflow)
        )
        return list(found or [])

    async def _dispatch(
        self, op: str, key: str, effect: EffectClass, perform: Any
    ) -> Any:
        """Through the broker, and deliberately **not** through the journal.

        Every other durable operation reaches a broker through
        :class:`DurableCall`, which journals it. State must not be journaled —
        it is shared across runs of a workflow and mutable, so a recorded value
        replayed later would be a lie about the present — and that is exactly
        why it reached no broker at all: the only path to one went through the
        journal.

        The consequence was that state was invisible to every broker.
        ``max_calls`` could not see a loop that wrote a key a million times;
        ``TaintBroker`` could not see a run reading data another run had put
        there; a host broker asked to log or refuse effects was never told. This
        separates the two questions the journal had bundled together: the broker
        decides whether a call may happen, the journal records that it did.
        """
        runtime = self._ctx._runtime
        broker = getattr(runtime, "broker", None)
        if broker is None:
            return await perform()

        from loom.runtime.effects import EffectCall

        target = f"state:{self._workflow}" + (f":{key}" if key else "")
        call = EffectCall(kind="state", target=target, effect=effect, perform=perform)
        result = await broker.dispatch(call, self._ctx._authority)
        if result.ok is False:
            raise EffectDenied(
                result.error or f"state {op} refused",
                call=call,
                needs=result.needs or "",
            )
        return result.value

    @property
    def _store(self) -> Any:
        return self._ctx._runtime.state

    @property
    def _workflow(self) -> str:
        return self._ctx.workflow



def _as_declared(produced: Any, output_type: type[BaseModel], node_id: str) -> Any:
    """Return *produced* as the node's declared ``Output``, or raise.

    A node whose body returns the wrong shape must fail here rather than hand
    back something that looks typed and is not. That is the defect
    ``coerce_output`` exists to prevent on the adapter side: the caller believes
    it holds a model, and finds out several attribute accesses later somewhere
    unrelated.

    A plain dict is validated rather than refused — returning a literal is a
    natural thing to write and the declared model makes it checkable — but
    anything else is a contract error.
    """
    if isinstance(produced, output_type):
        return produced
    if isinstance(produced, dict):
        try:
            return output_type.model_validate(produced)
        except PydanticValidationError as exc:
            raise NodeContractError(
                f"{node_id} returned a dict that does not fit "
                f"{output_type.__name__}: {exc}"
            ) from exc
    raise NodeContractError(
        f"{node_id} declares Output={output_type.__name__} and its run() returned "
        f"{type(produced).__name__}. The declared models are the node's contract; "
        "returning something else makes every caller's type annotation a lie."
    )


#: Where a caller's scopes are recorded, beside
#: ``loom.identity.facade.PRINCIPAL_KEY``. On the *record* so the
#: narrowing survives a park: a run resumed by a timer has no caller.
SCOPES_KEY = "loom.scopes"


def _authority_for(
    runtime: Runtime,
    definition: WorkflowDefinition[Any, Any, Any],
    record: ExecutionRecord | None = None,
) -> Authority:
    """What a run of *definition* on *runtime* is permitted to do.

    Note what is *not* here: the workflow's declared grants never override the
    Runtime's. They are consulted only when the Runtime declared none, where
    they act as a self-limitation. A workflow that could widen its own
    permissions by asking for more would make the declaration worthless.

    The *caller's* scopes narrow it further. ``scopes_to_grant`` existed for
    exactly this and was called from nowhere — so a token scoped to
    ``jira:read`` started runs with the workflow's full declaration, and the
    scope on the token described nothing that happened. Read from the record
    rather than passed in, because a run resumed from a timer or an event has
    no caller present to ask: the authority a run executes under has to survive
    parking, and the record is the only thing that does.
    """
    base = runtime.authority or Authority()
    if base.grant.is_empty and definition.grants is not None:
        base = base.narrowed(grant=definition.grants)

    scopes = (record.metadata or {}).get(SCOPES_KEY) if record is not None else None
    if not scopes:
        return base

    from loom.identity.scopes import scopes_to_grant

    # Narrows, never widens — the property `tests/test_identity.py` checks with
    # Hypothesis. Applied after the self-limitation above so a token can only
    # reduce what the workflow already asked for.
    return base.narrowed(grant=scopes_to_grant(frozenset(scopes), base.grant))


#: The branch scope in force for the *current task*, and which Context it
#: belongs to.
#:
#: A ``contextvars.ContextVar`` rather than an attribute, because the coroutine
#: that has to be redirected already closed over the parent ``Context``:
#: ``ctx.gather(branch_a(), branch_b())`` hands `gather` two coroutines that
#: will call ``ctx.step(...)`` on the *same* object. `asyncio` copies the
#: current context when a task is created, so a value set inside one branch's
#: task is invisible to its siblings — which is exactly the isolation needed
#: and exactly what an attribute could not provide.
#:
#: Paired with the owning Context so that a ``nested()`` view created *inside*
#: a branch keeps its own numbering: the override applies to the object that
#: opened it, never to a different one that merely runs beneath it.
_BRANCH_SCOPE: ContextVar[tuple[object, Scope] | None] = ContextVar(
    "loom_branch_scope", default=None
)


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
        credentials: Any = None,
        env: Any = None,
    ) -> None:
        self._runtime = runtime
        self._record = record
        self._journal = journal
        self._definition = definition
        self._deps = deps
        self._root_scope = scope or journal.root
        self.logger = logger or logging.getLogger(f"workflow.{record.workflow}")
        self._tracer = runtime.tracer
        self._clock = runtime.clock
        """Every timestamp this run writes and every wait it holds in memory.

        Bound once rather than read through the Runtime each time, so a test
        that swaps the clock does so before the run starts and cannot change it
        underneath a run already in flight."""
        self._credentials = credentials
        """The :class:`~loom.connectors.credentials.CredentialStore`
        bound for this run — per-run tokens layered over the Runtime's store.
        ``None`` preserves the pre-existing env-var fallback in toolsets."""
        if env is not None:
            self.env = env
        else:
            from loom.runtime.environment import RunEnvironment

            self.env = RunEnvironment()
        """Per-run environment overrides. See :class:`RunEnvironment`."""
        self._authority = _authority_for(runtime, definition, record)
        self._grant_override: Any = None
        """Grant narrowed by an enclosing call, inherited by nested ones.

        ``None`` means "whatever the workflow declared". Set only by
        :meth:`_effective_grant`'s callers, and only ever to something smaller.
        """
        """What this run may do, handed to the broker on every dispatch.

        The Runtime's authority, falling back to what the workflow declared it
        needs — a workflow can narrow itself, but nothing it says can widen the
        Runtime."""
        self._backoff_rng = _random.Random(record.run_id)
        self._pending_flush = 0
        self._compensation_stack: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...]]] = []
        self._active_patches: frozenset[str] = frozenset()
        self._warned_journal_size = False
        self._warned_payload_size = False

    @property
    def _scope(self) -> Scope:
        """Where this context's next durable call takes its path from.

        The root scope, unless a concurrent branch opened by :meth:`gather` is
        running in this task — see :data:`_BRANCH_SCOPE`.

        This indirection is the fix for a defect at the centre of the engine.
        Paths were allocated from one counter shared by every branch, at the
        moment a call was *constructed*, so under `gather` the numbering
        depended on how long the previous step took: change a latency and two
        logically distinct call sites swap paths. On replay each was then
        served the other's recorded value — silently, because the default
        verify mode logged and served it anyway, and the run reported
        ``completed`` with different output.
        """
        override = _BRANCH_SCOPE.get()
        if override is not None and override[0] is self:
            return override[1]
        return self._root_scope

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
            credentials=self._credentials,
            env=self.env,
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
        definition: StepDefinition[Any, Any] = (
            target
            if isinstance(target, StepDefinition)
            else StepDefinition(fn=target, name=name or callable_name(target, "anonymous"))
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
            # Bound to the step's own signature, so a credential passed
            # positionally — which is how every one of them is actually passed —
            # is matched by the parameter it lands on rather than slipping past
            # a denylist that can only see keys.
            input=redact_call_input(
                definition.fn, args, kwargs, self._runtime.redact_keys
            ),
            output_type=definition.output_type,
            retry=effective_retry,
            timeout=definition.timeout if timeout is None else timeout,
            on_error=on_error or definition.on_error,
            fallback=definition.fallback if fallback is None else fallback,
            metadata={
                "concurrency_key": definition.concurrency_key,
                **self._declared_effect(step_name),
            },
        )

    def _declared_effect(self, step_name: str) -> dict[str, Any]:
        """What this step's manifest says it is, when a manifest knows it.

        A toolset operation's :class:`EffectClass` lives on its ``OperationSpec``
        and nothing carried it to the call site, so ``ctx.step(gmail_search…)``
        reached the broker as an unnamed *write*. Anything deciding on reads
        versus writes — taint most of all — was therefore deciding on a default
        rather than on the declaration, and a read could never taint.

        A plain local ``@step`` stays unclassified and keeps the write default:
        it is not a declaration a manifest ever made, and inventing one here
        would guess at the very thing the manifest exists to state.
        """
        registry = getattr(self._runtime, "toolsets", None)
        resolve = getattr(registry, "profile_of", None)
        if resolve is None:
            # A registry predating profiles still answers the narrow question.
            legacy = getattr(registry, "effect_of", None)
            if legacy is None:
                return {}
            declared = legacy(step_name)
            return {"effect_class": declared} if declared is not None else {}
        profile = resolve(step_name)
        if profile is None:
            return {}
        # Every facet, not just the class: the grant rule asks how much damage,
        # the taint rule asks whether it came from outside and whether anything
        # undoes it. One lookup, because a facet added later should not mean
        # editing this call site again.
        return {
            "effect_class": profile.effect,
            "effect_by": profile.effect_by,
            "open_world": profile.open_world,
            "reversible": profile.reversible,
            "access_control": profile.access_control,
        }

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
            # The journal just gained an answer a broker's decision may turn on
            # — an approval clearing taint, most of all. Events are journaled
            # here and never dispatched, so without this the only broker that
            # would ever see one is a broker that reads the journal.
            self._runtime.observe_run(self.run_id, self._journal)
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

    def _next_chain_depth(self) -> int:
        """How many workflow-to-workflow hops a newly published event carries.

        One more than the depth of the event that triggered *this* run
        (``loom.chain_depth``, set by ``EventDispatcher`` at submit), or ``0``
        when this run was not itself started from an event — it is then the
        first hop of whatever chain it starts, not the continuation of one.
        """
        depth = self._record.metadata.get("loom.chain_depth")
        return int(depth) + 1 if depth is not None else 0

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

        When the Runtime carries an :class:`~loom.events.log.EventLog`
        (``Runtime(events=...)``), this also appends the event under topic
        ``name`` — the same log an :class:`~loom.events.dispatcher.EventDispatcher`
        reads, so a workflow's own event can start or filter into another
        workflow's ``OnAppEvent`` subscription exactly as an external one would.
        Off unless an ``EventLog`` is configured, so a Runtime with none pays
        nothing extra here.
        """

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            await self._runtime.send_event(
                None,
                name,
                payload,
                to_topic=True,
                event_id=f"loom:publish:{self.run_id}:{step_ctx.path}",
                chain_depth=self._next_chain_depth(),
            )
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

        prepared = [self._as_branch(index, item) for index, item in enumerate(items)]
        tasks = [asyncio.ensure_future(guarded(item)) for item in prepared]
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

    def _as_branch(self, index: int, item: Awaitable[Any]) -> Awaitable[Any]:
        """Give *item* its own numbering space, when it needs one.

        A :class:`DurableCall` is left exactly as it is: its path was fixed
        when it was constructed, which happened in argument order, before
        `gather` ever saw it. ``ctx.gather(ctx.step(a), ctx.step(b))`` is
        therefore already deterministic and must keep the paths it has, or
        every journal written before this change stops replaying.

        Anything else is a coroutine that will allocate *while it runs*, and
        that is the shape the ordering defect lives in. It gets a prefix
        allocated here — synchronously, in argument order, before any of them
        starts — so its internal calls number within the branch and cannot
        interleave with a sibling's.
        """
        if isinstance(item, DurableCall):
            return item
        del index  # position is expressed by allocation order, not by value
        return self._in_branch(self._scope.allocate(), item)

    async def _in_branch(self, base: str, awaitable: Awaitable[Any]) -> Any:
        """Run *awaitable* with durable calls numbered beneath *base*."""
        token = _BRANCH_SCOPE.set((self, Scope(prefix=f"{base}.")))
        try:
            return await awaitable
        finally:
            _BRANCH_SCOPE.reset(token)

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
            child_kwargs: dict[str, Any] = {
                "parent_run_id": self.run_id,
                "deps": self._deps,
            }
            parent_store = self._runtime._run_credentials.get(self.run_id)
            if parent_store is not None:
                child_kwargs["credentials"] = parent_store
            env_overrides = self.env.overrides()
            if env_overrides:
                child_kwargs["env"] = env_overrides
            if detached:
                return await self._runtime.submit(definition, input, **child_kwargs)
            result = await self._runtime.run(
                definition,
                input,
                root_run_id=self._record.root_run_id or self.run_id,
                **child_kwargs,
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
        agent_or_prompt: Agent[T],
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
        agent_or_prompt: Agent[T] | str,
        input: Any = None,
        *,
        name: str | None = None,
        max_turns: int | None = None,
        session_id: str | None = None,
        agent_id: str = "",
        toolsets: list[str] | None = None,
        grants: Any = None,
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
        grants:
            Optional :class:`~loom.security.grants.GrantSet`
            narrowing what *this* call may reach. Intersected with the grant
            already in force, never unioned — a call cannot hand itself
            authority its caller does not hold, and a nested ``ctx.agent()``
            inherits the narrowed set rather than the workflow's declaration.
        """
        if isinstance(agent_or_prompt, str):
            if input is not None:
                # Previously this fell through to the Agent path below, which
                # immediately read `.name` off the string: `ctx.agent("...",
                # input=x)` died with `AttributeError: 'str' object has no
                # attribute 'name'` three frames down. The backend path is
                # prompt-only by construction — there is nowhere to put a
                # second argument — so this is a caller mistake, and it should
                # say so rather than crash somewhere else.
                raise ConfigurationError(
                    "ctx.agent(<prompt>) takes no input=: a prompt string is the "
                    "whole request, and the runtime's AgentBackend has nowhere to "
                    "put a second argument. Put the data in the prompt, or pass an "
                    "Agent object, which does take input."
                )
            return self._agent_from_backend(
                agent_or_prompt,
                name=name,
                toolsets=toolsets,
                session_id=session_id,
                max_turns=max_turns,
                agent_id=agent_id,
                grants=grants,
            )

        # Backward-compatible path: Agent object
        from loom.agents.runner import run_agent_durably

        agent = agent_or_prompt
        label = name or f"agent:{agent.name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            return await run_agent_durably(
                agent,
                input,
                ctx=self.nested(step_ctx.path),
                max_turns=max_turns,
                session_id=session_id,
                authority=self._authority_with(self._effective_grant(grants)),
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

    def _effective_grant(self, requested: Any = None) -> Any:
        """The grant in force for one call: declared, then narrowed.

        Narrowing only. ``GrantSet.intersect`` is provably a subset of both
        operands, so a call cannot widen what its caller holds no matter what
        it asks for — which is the property that makes the parameter safe to
        expose to generated code and to a nested agent.

        The narrowed set is carried on the Context, so a sub-workflow or a
        nested ``ctx.agent()`` inherits it rather than reaching back to the
        workflow's declaration. Without that, narrowing would last exactly one
        call deep, which is the same as not narrowing at all.
        """
        declared = self._grant_override or self._definition.grants
        if requested is None:
            return declared
        if declared is None:
            return requested
        return declared.intersect(requested)

    def _authority_with(self, grant: Any) -> Any:
        """This context's authority, narrowed to *grant*.

        Returned rather than assigned: the broker weighs every dispatch against
        whatever authority it is handed, and two ``ctx.agent()`` calls running
        under ``gather`` must not be able to observe each other's narrowing.
        """
        if grant is None or grant is self._definition.grants:
            return self._authority
        return self._authority.narrowed(grant=grant)

    def _agent_from_backend(
        self,
        prompt: str,
        *,
        name: str | None = None,
        toolsets: list[str] | None = None,
        session_id: str | None = None,
        max_turns: int | None = None,
        agent_id: str = "",
        grants: Any = None,
    ) -> DurableCall[AgentResult[Any]]:
        """Route a prompt-only ctx.agent() call through the runtime's backend.

        The backend is checked inside ``perform``, not here. Both are the same
        error at the same moment for a call that actually executes — but a call
        whose result is already journaled never reaches ``perform``, because
        ``DurableCall._resolve`` serves the recorded entry first. Raising at
        construction made a *replay* demand the ability to recompute an answer
        the journal already holds, so a finished run could not be replayed or
        resumed in a worker with no model configured — which is what a CI
        replay, and any process that only re-drives parked runs, is.
        """
        identity = agent_id or "backend"
        label = name or f"agent:{identity}"

        # Sessions are keyed by agent as well as conversation, so two agents
        # sharing a session id keep separate memories of it.
        memory_key = f"{identity}:{session_id}" if session_id else None

        def resolved_backend() -> Any:
            backend = self._runtime.agent_backend
            if backend is None:
                raise ConfigurationError(
                    "ctx.agent('prompt') requires an agent_backend on the Runtime. "
                    "Pass agent_backend=... to Runtime() or use "
                    "ctx.agent(Agent(...), input) instead."
                )
            if session_id is not None and not getattr(backend, "supports_history", False):
                raise ConfigurationError(
                    f"{type(backend).__name__} does not support conversation history, so "
                    f"session_id={session_id!r} would be ignored and every call would "
                    "start from a blank conversation. Use BuiltInBackend, or drop "
                    "session_id."
                )
            return backend

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            backend = resolved_backend()
            # Layer 3: resolve tools lazily from the registry, narrowed to what
            # this workflow declared it may use.
            tools = self._runtime.toolsets.resolve_tools(
                toolsets, grants=self._effective_grant(grants)
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

    # -- nodes --------------------------------------------------------------------------

    def node(
        self,
        node_id: str,
        payload: Any = None,
        *,
        name: str | None = None,
        guards: list[Any] | None = None,
        retry: Retry | int | None = None,
        timeout: Duration | None = None,
    ) -> DurableCall[Any]:
        """Run a catalogued node: Pydantic in, Pydantic out.

            decision = await ctx.node("human.approval", ApprovalIn(subject="refund"))

        Nodes add no durability semantics. This journals exactly what the
        equivalent hand-written code would, and a node that parks the run raises
        ``Suspend`` the same way ``ctx.wait_for_approval`` does.

        Two things happen **before** the call is journaled, deliberately: the
        node is resolved, and *payload* is validated against its declared
        ``Input``. A malformed payload is the caller's mistake, and surfacing it
        as a failed step puts the error arbitrarily far from its cause.

        Runtime **requirements** are checked one step later, inside the
        journaled call. Still before the body runs and before a suspending node
        parks — so a run never parks with nobody listening, which is the worst
        outcome available here because it is indistinguishable from patience.
        But a call whose answer is already recorded never reaches that check,
        so replaying a finished run no longer demands the capability that
        produced it: a completed ``human.review_edit`` replays in a process with
        no human channel, exactly as a completed ``ctx.agent`` replays in one
        with no model.
        """
        from loom.nodes.base import NodeContext

        registry = self._runtime.nodes
        node = registry.resolve(node_id)
        definition = type(node)
        spec = definition.spec

        try:
            validated = (
                payload
                if isinstance(payload, definition.Input)
                else definition.Input.model_validate(payload or {})
            )
        except PydanticValidationError as exc:
            raise NodeContractError(
                f"{node_id} was called with a payload that does not fit "
                f"{definition.Input.__name__}: {exc}"
            ) from exc

        applied_guards = list(guards) if guards is not None else list(spec.guards)
        label = name or f"node:{node_id}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            # Before the body, and before a suspending node parks — but only
            # for a call that is actually going to run. See the docstring.
            registry.check_requirements(node, self._runtime)
            scoped = self.nested(step_ctx.path)
            node_ctx = NodeContext(scoped)
            checked = validated
            if applied_guards:
                # Imported here rather than at module scope: the guard package
                # exports its node classes, so a top-level import would register
                # five nodes on `import loom` — before anything asked
                # for a catalog, and reported as already-present by the loader.
                from loom.nodes.guard.runner import apply_guards

                checked = await apply_guards(
                    applied_guards,
                    validated,
                    ctx=node_ctx,
                    registry=registry,
                    phase="input",
                    subject=node_id,
                )
            produced = await node.run(node_ctx, checked)
            return _as_declared(produced, definition.Output, node_id)

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (validated,)),
            input=validated,
            output_type=definition.Output,
            retry=(
                Retry(max_attempts=retry)
                if isinstance(retry, int)
                else retry or Retry(max_attempts=1)
            ),
            timeout=timeout,
            metadata={
                "effect_kind": "node",
                "node_id": node_id,
                "node_version": spec.version,
                "effect_class": spec.effect,
                "effect_by": spec.effect_by,
                "open_world": spec.open_world,
                "effect_target": node_id,
                # Journaled so a node upgraded between a run and its replay is
                # caught rather than decoding an old payload into a new model.
                "contract": spec.contract_hash,
            },
        )

    async def guard(self, guard_id: Any, value: Any = None) -> Any:
        """Check *value* against a guard, and return what the run should use.

        ALLOW returns *value* unchanged and REPLACE returns the substitute;
        REJECT and TRIPWIRE raise. Returning a falsy verdict a caller could
        ignore is the one behaviour a guard must not have outside an agent loop,
        where there is no model to hand the explanation to.
        """
        from loom.nodes.base import NodeContext
        from loom.nodes.guard.runner import apply_guards

        return await apply_guards(
            [guard_id],
            value,
            ctx=NodeContext(self),
            registry=self._runtime.nodes,
            phase="standalone",
            subject=getattr(guard_id, "name", str(guard_id)),
            # A guard input carries configuration around the thing being
            # checked; what the run should use afterwards is the thing.
            unwrap=True,
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
            if isinstance(payload, Attachment):
                if payload.is_offloaded:
                    raw = await payload.read(self._runtime.blobs)
                else:
                    raw = payload.data or b""
                content_type = payload.mime
                extra = {**payload.metadata, **metadata}
            else:
                raw = payload
                content_type = mime
                extra = metadata
            return await service.put(
                name, raw, mime=content_type, run_id=self.run_id, **extra
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

        A replay reads exactly what the original run read, even if newer
        versions have been published since — without that, a replay would be a
        rehearsal of what would happen now rather than of what happened.

        **It is the content that is journaled, not the version.** That is worth
        stating plainly because the guarantee sounds like it should cost a
        version number and instead costs the bytes: reading a 500 MB artifact
        writes ~667 MB into the journal, base64 included. With
        ``Runtime(blobs=...)`` the payload goes back out to blob storage over
        the offload threshold — and content-addressing means it dedupes against
        the artifact's own bytes, so the cost is a reference. Without a blob
        service it is inline, and ``journal_max_payload_bytes`` is what stops it.

        For large artifacts prefer :meth:`artifact_url`, which is deliberately
        not journaled, or pass the reference to a step that streams it.
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

    def stage_artifact(
        self,
        name: str,
        data: bytes | Attachment,
        *,
        mime: str = "application/octet-stream",
        **metadata: Any,
    ) -> DurableCall[Any]:
        """Stage a file for later commit as a versioned artifact.

        Journaled. On replay, returns the same staged entry without re-staging.
        Bytes go to blob storage immediately so a crash does not lose them.
        An already-offloaded :class:`Attachment` reuses its ``ref``.
        """
        from loom.blobs.staging import StagedArtifact

        payload = data
        label = f"artifact:stage:{name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            staging = self._runtime.require_staging()
            return await staging.stage(
                name, payload, mime=mime, run_id=self.run_id, metadata=metadata
            )

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (name,)),
            input={"name": name, "mime": mime},
            output_type=StagedArtifact,
        )

    def commit_staged(
        self, name: str, *, labels: dict[str, str] | None = None
    ) -> DurableCall[ArtifactVersion]:
        """Promote a staged artifact to a versioned artifact.

        Journaled. On replay, returns the same :class:`ArtifactVersion`.
        """
        from loom.blobs.staging import StagingNotFound

        label = f"artifact:commit:{name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            try:
                return await self._runtime.require_staging().commit(
                    name, run_id=self.run_id, labels=labels
                )
            except StagingNotFound as exc:
                raise StepError(str(exc), step_name=label) from exc

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (name,)),
            input={"name": name},
            output_type=ArtifactVersion,
        )

    def discard_staged(self, name: str) -> DurableCall[None]:
        """Drop a staged artifact. Journaled for replay consistency."""
        label = f"artifact:discard:{name}"

        async def perform(attempt: int, step_ctx: StepContext) -> Any:
            await self._runtime.require_staging().discard(name, run_id=self.run_id)
            return None

        return DurableCall(
            self,
            kind=EntryKind.STEP,
            name=label,
            perform=perform,
            fingerprint=make_fingerprint(label, (name,)),
            input={"name": name},
            output_type=type(None),
        )

    async def artifact_url(
        self,
        name: str,
        version: int | None = None,
        *,
        expires_in: int = 3600,
    ) -> str:
        """Generate a presigned download URL for an artifact.

        Not replay-stable: signed URLs expire, so a journaled URL would be
        dead by the time a replay ran. Generating a fresh one is side-effect
        free and is the correct behaviour on re-entry.
        """
        url: str = await self._runtime.require_artifacts().url(
            name, version, expires_in=expires_in
        )
        return url

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
        # A @step is the obvious thing to reach for here, and it has no
        # __name__ — which surfaced as an AttributeError inside the failure
        # path, i.e. as a second, unrelated-looking failure while the first was
        # being unwound.
        label = getattr(fn, "name", None) or getattr(fn, "__name__", None) or repr(fn)
        recorded = self._journal.lookup(path, EntryKind.STEP, f"compensate:{label}")
        if recorded is not None and recorded.status is EntryStatus.COMPLETED:
            # Already registered on a prior attempt — rebuild the stack
            self._compensation_stack.append((fn, args))
            return

        self._compensation_stack.append((fn, args))
        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.STEP,
                name=f"compensate:{label}",
                status=EntryStatus.COMPLETED,
                output=_encode_debug(
                    {"fn": label, "args": args}, self._runtime.redact_keys
                ),
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
                label = (
                    getattr(fn, "name", None) or getattr(fn, "__name__", None) or repr(fn)
                )
                self.logger.error("compensation %s failed: %s", label, exc)
                failures.append(label)
        self._compensation_stack.clear()
        return failures

    # -- version gates ------------------------------------------------------------------

    def patched(self, name: str) -> bool:
        """Version gate: introduce a branch without breaking runs already going.

        ::

            if ctx.patched("use-new-pricing"):
                total = await ctx.step(price_v2, cart)
            else:
                total = await ctx.step(price_v1, cart)

        A run that reaches this gate for the first time records that the patch
        was present and takes the new branch, and keeps taking it on every
        later replay. A run that was *already past this point* before the
        branch existed takes the old one — forever — because its journal proves
        it did.

        That is what makes the two safe to deploy at once. Without it, adding a
        branch changes what an in-flight run does halfway through, which is the
        one thing replay is supposed to rule out.

        The marker is keyed by *name*, not by position, so inserting a durable
        call before the gate does not change which decision it finds. That is
        deliberately unlike every other entry in the journal, and it is the
        reason a patch id must be unique within a workflow and must never be
        reused for a different change.

        Args:
            name: A stable id for this change. Keep it after the old branch is
                deleted for as long as any suspended run might still hold the
                marker; ``loom runs`` is how you find out.
        """
        marker = f"patch:{name}"
        recorded = self._journal.find(EntryKind.SIDE_EFFECT, marker)
        if recorded is not None:
            return bool(recorded.output)

        # No marker. Either this run has never been here, or it got past this
        # point before the gate existed. The journal answers that: an entry
        # allocated after this position means the body already ran through here
        # under the old code.
        path = self._scope.allocate()
        predates = self._journal.has_entries_after(path)
        active = not predates
        self._journal.put(
            JournalEntry(
                path=path,
                kind=EntryKind.SIDE_EFFECT,
                name=marker,
                status=EntryStatus.COMPLETED,
                output=active,
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
                attempts=1,
            )
        )
        return active

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

    def park_if_paused(self) -> None:
        """Suspend now if an operator has asked this run to hold.

        Called at the same boundary cancellation is checked at — after every
        already-answered call has been served from the journal, and before a new
        side effect is performed. That is what makes a pause safe: nothing is
        half-done, so the run resumes exactly where a crash would have.
        """
        if self._runtime.is_pause_requested(self.run_id):
            raise Suspend(
                f"run {self.run_id} is paused",
                path=self._scope.prefix,
                awaiting_event=f"resume:{self.run_id}",
            )

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
            # No blob service is the *default*, and it used to mean no sizing at
            # all — a step returning 200 MB was written to the journal verbatim.
            # The entry budget could not see it either, because it counts
            # entries and one enormous entry is `1`. So the only bound on a
            # journal row was whatever the driver happened to raise: an opaque
            # `DocumentTooLarge` on Mongo at 16 MB, and on SQLite or Postgres no
            # error at all — just a row that is re-read and re-parsed on every
            # subsequent replay, degrading exactly the way the entry budget
            # exists to prevent while being invisible to it.
            self._check_payload_size(encoded)
            return encoded
        raw = _json_bytes(encoded)
        if raw is None or not blobs.should_offload(raw):
            return encoded
        ref = await blobs.store(raw, "application/json")
        self.logger.debug("offloaded %d bytes to %s", len(raw), ref)
        return {BLOB_KEY: ref}

    async def _load_payload(self, stored: Any, *, where: str = "") -> Any:
        """Rehydrate a journal payload, following a blob reference if present."""
        if not _is_blob_marker(stored):
            return stored
        ref = stored[BLOB_KEY]
        at = f" at {where}" if where else ""
        blobs = self._runtime.blobs
        if blobs is None:
            raise ConfigurationError(
                f"journal entry references {ref} but this Runtime has no blob service. "
                "Pass the same blobs=BlobService(...) used when the run was recorded."
            )
        try:
            return json.loads(await blobs.load(ref))
        except BlobNotFoundError as exc:
            # A journal entry naming a blob that is gone is unrecoverable, and
            # the raw error is a bare 64-character hash — no run, no step, and
            # nothing suggesting where it went. Worse, it was reported
            # `retryable=True`, so a run whose payload no longer exists was
            # retried forever against a blob that will never come back.
            #
            # The usual cause is retention: `_drop_blobs` deletes by scanning
            # one run's journal, and blobs are content-addressed, so a replay
            # clone — or any run that produced identical bytes — shares the ref
            # and loses it when the first of them is compacted.
            raise DataUnavailable(
                f"run {self.run_id!r}{at} recorded its output as {ref}, and "
                "that blob no longer exists. A journaled payload cannot be "
                "reconstructed, so this run cannot replay. The usual cause is "
                "retention compaction deleting a blob that another run still "
                "referenced — blobs are content-addressed, so runs producing "
                "identical bytes share one. Check RetentionManager settings, "
                "and whether this run is a replay clone of an "
                "already-compacted run."
            ) from exc

    def _check_payload_size(self, encoded: Any) -> None:
        """Warn, then fail, on a single journal payload that is too large.

        Mirrors :meth:`_check_journal_size` one level down: that one bounds how
        *many* entries a run writes, this one bounds how *big* one of them is.
        Both were needed and only the first existed.

        Only reached when no :class:`BlobService` is configured — with one, a
        payload over the offload threshold becomes a `blob:` reference and never
        gets here. So the fix this names is the real one.
        """
        runtime = self._runtime
        limit = getattr(runtime, "journal_max_payload_bytes", 0)
        warn_at = getattr(runtime, "journal_warn_payload_bytes", 0)
        if not limit and not warn_at:
            return

        raw = _json_bytes(encoded)
        if raw is None:
            return
        size = len(raw)

        if limit and size >= limit:
            raise BudgetExceeded(
                f"run {self.run_id} tried to journal a {size:,}-byte payload, over "
                f"the limit of {limit:,}. Configure Runtime(blobs=BlobService(...)) "
                "so payloads this size are stored by content hash and referenced "
                "from the journal, put the data in an artifact "
                "(ctx.put_artifact), or raise journal_max_payload_bytes.",
                budget_type="journal_payload_bytes",
                limit=limit,
                actual=size,
            )

        if warn_at and size >= warn_at and not self._warned_payload_size:
            self._warned_payload_size = True
            self.logger.warning(
                "run %s journaled a %d-byte payload with no blob service "
                "configured; it is stored inline and re-read on every replay",
                self.run_id,
                size,
            )

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
