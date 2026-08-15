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
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loom.core.exceptions import (
    AdmissionRejected,
    AuthExpired,
    ConfigurationError,
    ContinueAsNew,
    InputMismatch,
    RegistryError,
    Suspend,
    WorkflowCancelled,
)
from loom.core.models import (
    ErrorInfo,
    Event,
    ExecutionRecord,
    ExecutionResult,
    ExecutionStatus,
    TriggerKind,
)
from loom.core.secret import Secret
from loom.core.serde import decode, encode
from loom.core.types import Duration, to_seconds
from loom.identity.principal import ServicePrincipal
from loom.observability.tracing import NoopTracer, Tracer
from loom.runtime.backend import DurabilityBackend, EmbeddedBackend
from loom.runtime.clock import Clock, SystemClock
from loom.runtime.context import Context
from loom.runtime.effects import DirectBroker, EffectBroker
from loom.runtime.flowcontrol import AdmissionController, AdmissionDecision
from loom.runtime.journal import (
    CompatibilityMode,
    EntryStatus,
    Journal,
    VerifyMode,
)
from loom.runtime.leader import LeaderElector
from loom.runtime.registry import WorkflowRecord
from loom.runtime.state import (
    InMemoryRunStream,
    RunStream,
    StateStore,
    StoreBackedState,
)
from loom.runtime.workflow import WorkflowDefinition
from loom.security.authority import Authority
from loom.security.rbac import Permission, Role, require
from loom.stores.memory import MemoryStore

logger = logging.getLogger("workflow.engine")


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on comparison.

    SQLite and some drivers hand back naive datetimes even for values written
    with a timezone.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
        credential_resolver: Any | None = None,
        env: dict[str, str] | None = None,
        service_principal: ServicePrincipal | None = None,
        deps: Any = None,
        agent_backend: Any | None = None,
        blobs: Any | None = None,
        toolsets: Any | None = None,
        nodes: Any | None = None,
        human: Any | None = None,
        sessions: Any | None = None,
        admission: AdmissionController | None = None,
        role: Role | None = None,
        authority: Authority | None = None,
        broker: EffectBroker | None = None,
        clock: Clock | None = None,
        state: StateStore | None = None,
        stream: RunStream | None = None,
        artifacts: Any | None = None,
        signed_urls: Any | None = None,
        staging: Any | None = None,
        catalog: Any | None = None,
        node_id: str | None = None,
        lease_ttl: Duration = 60.0,
        journal_warn_entries: int = 5_000,
        journal_max_entries: int = 50_000,
        inline_timer_threshold: Duration = 2.0,
        max_inline_wait: Duration = 0.0,
        flush_every: int = 1,
        compatibility: CompatibilityMode = CompatibilityMode.STRICT,
        verify: VerifyMode = VerifyMode.WARN,
        validate_input: bool = True,
        spill: Any | None = None,
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
        self.credential_resolver = credential_resolver
        """Optional async callable ``(ExecutionRecord) -> CredentialStore | None``.
        Re-derives per-run credentials on resume, including on another node."""
        self.env: dict[str, str] = dict(env or {})
        """Runtime-level environment defaults, overridden by ``run(env=...)``."""
        self._run_credentials: dict[str, Any] = {}
        """In-memory per-run credential stores, keyed by run id. Cleared when
        the run goes terminal. Survives a park in this process; a resolver
        covers a resume elsewhere."""
        self.service_principal = service_principal or ServicePrincipal(subject="scheduler")
        """The identity a trigger-fired run submits under (``record.metadata``),
        since a cron/interval trigger has no interactive caller to ask. Only
        ``TriggerDispatcher`` reads this — a manually or MCP/HTTP-submitted run
        keeps whatever principal (or none) its own caller pinned."""
        self.deps = deps
        self.agent_backend = agent_backend
        self.blobs = blobs
        """Optional :class:`BlobService`. When set, journal payloads over its
        threshold are stored by content hash and referenced from the journal."""

        if toolsets is not None:
            self.toolsets = toolsets
        else:
            from loom.agents.tool_registry import ToolsetRegistry
            from loom.toolsets.registry import get_catalog

            # Chains to the process-global registry, so anything registered with
            # register_toolset() or a loom_toolset entry point is reachable here
            # — while registrations made on this Runtime stay local to it.
            self.toolsets = ToolsetRegistry(parent=get_catalog())

        if nodes is not None:
            self.nodes = nodes
        else:
            from loom.nodes.registry import (
                NodeRegistry,
                get_node_catalog,
                load_builtin_nodes,
                load_node_entry_points,
            )

            # Same arrangement as toolsets: chains to the process-global catalog
            # so @register_node and loom_node entry points reach every Runtime,
            # while rt.nodes.register(...) stays local to this one. Entry points
            # load here rather than at import, so `import loom` does
            # not pull in every installed node package.
            load_builtin_nodes()
            load_node_entry_points()
            self.nodes = NodeRegistry(parent=get_node_catalog())

        self.human = human
        """Optional :class:`HumanChannel` — how a ``human.*`` node reaches a
        person. LOOM owns parking the run and validating the answer; delivery is
        the provider's. Without one, a ``human.*`` node raises before the run
        parks rather than parking with nobody listening."""

        if sessions is not None:
            self.sessions = sessions
        else:
            from loom.agents.memory import StoreBackedSession

            # Backed by the execution store, so an agent's memory survives a
            # restart exactly as the journal does.
            self.sessions = StoreBackedSession(self.store)
        self.admission = admission
        """Optional :class:`AdmissionController`. Required for a workflow's
        ``flow_control`` policy to have any effect."""
        self.role = role
        """Optional :class:`Role` this Runtime acts as. ``None`` means no
        authorization is enforced — checks are opt-in so embedded use stays
        ceremony-free, and a role that is set is always checked."""

        self.authority = authority
        """Optional default :class:`Authority` — what runs here may invoke, and
        whether they are rehearsing. ``None`` restricts nothing, which is what
        an embedded Runtime wants and what the effect broker's cheap path
        assumes. A workflow's own ``grants=`` applies when this is unset."""

        self.clock: Clock = clock or SystemClock()
        """Where every timestamp and every in-memory wait comes from.

        A :class:`ManualClock` makes timers and cron triggers testable without
        waiting for them — see :func:`loom.testing.advance`. Runs
        bind it when they start, so swapping it mid-run changes nothing."""

        self.broker: EffectBroker = broker or DirectBroker()
        """Mediates every durable operation. The default performs them and
        checks nothing, so the seam exists without costing anything; swap in a
        :class:`GuardedBroker` to enforce an authority's grant, a call ceiling,
        or a dry run."""

        self.state: StateStore = state or StoreBackedState(self.store)
        """Backs ``ctx.state`` — workflow-scoped key-value state that outlives a
        run. Defaults to the execution store, so it needs nothing new."""

        self.stream: RunStream = stream or InMemoryRunStream()
        """Where ``ctx.report`` goes. The default buffers in memory and is
        readable through the facade, so ``loom watch`` shows progress with no
        host involvement — and nothing crosses a process boundary until a host
        supplies an adapter that does."""

        self.artifacts = artifacts
        """Optional :class:`ArtifactService`. When ``blobs`` is configured and
        this is not, one is built over it with a store-backed index."""
        if self.artifacts is None and self.blobs is not None:
            from loom.blobs.artifact import (
                ArtifactService,
                StoreBackedArtifactStore,
            )

            self.artifacts = ArtifactService(
                self.blobs, StoreBackedArtifactStore(self.store)
            )

        self.staging = staging
        """Optional :class:`~loom.blobs.staging.StagingManager`.
        Built automatically when blobs and artifacts are both available."""
        if self.staging is None and self.artifacts is not None and self.blobs is not None:
            from loom.blobs.staging import StagingManager

            self.staging = StagingManager(self.blobs, self.artifacts, self.store)

        self.signed_urls = signed_urls
        """Optional :class:`~loom.blobs.signed_urls.SignedUrlService`.
        Built automatically when blobs are configured."""
        if self.signed_urls is None and self.blobs is not None:
            from loom.blobs.signed_urls import SignedUrlService

            self.signed_urls = SignedUrlService(self.blobs, self.store)

        if catalog is not None:
            self.catalog = catalog
        else:
            from loom.runtime.registry import StoreBackedWorkflowRegistry

            self.catalog = StoreBackedWorkflowRegistry(self.store)
        """Published workflow catalog. Nothing is written until :meth:`publish`
        is called — importing a module never touches storage."""

        self.node_id = node_id or f"node-{uuid.uuid4().hex[:12]}"
        """Identifies this process in run leases, so an orphaned run can be told
        apart from one another node is actively working on."""
        self.lease_ttl = to_seconds(lease_ttl)
        """How long a run lease stays valid without a heartbeat."""

        self.journal_warn_entries = journal_warn_entries
        """Log once when a run's journal passes this. A long-lived flow that never
        rotates degrades slowly and silently; this makes it visible early."""
        self.journal_max_entries = journal_max_entries
        """Fail the run when its journal passes this. Set to 0 to disable.

        Failing loudly beats replaying a million entries on every attempt until
        the process runs out of memory."""

        self.compatibility = compatibility
        self.verify = verify
        """Whether a replayed entry must prove it belongs to the call that found it.

        Defaults to :attr:`VerifyMode.WARN`: a difference is reported and the
        recorded value is still served. Arguments read from ``ctx.state`` — not
        journaled by design — legitimately differ across replays, so raising by
        default would break correct workflows to catch an uncommon one.
        """
        self.validate_input = validate_input
        """Whether a payload is checked against ``input_schema()`` before a run opens.

        On by default, because a shape mismatch that reaches a step body
        surfaces as an ``AttributeError`` several operations in, which reads as
        a broken workflow rather than a wrong input. Turn it off for a codebase
        with decorative annotations — a parameter declared ``str`` that the body
        ignores and callers fill with anything — where the declaration was never
        meant as a contract.
        """
        if spill is not None:
            self.spill = spill
        elif self.blobs is not None:
            from loom.agents.bounds import BlobSpillStore

            self.spill = BlobSpillStore(self.blobs)
        else:
            self.spill = None
        """Where an oversized tool result is stored so the model can page it.

        Defaults to the blob service when one is configured: a deployment that
        has somewhere to put large values has already said so once, and asking
        twice is how two settings drift apart. Without blobs there is no store,
        and bounding degrades to truncation with an honest notice.
        """
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

    @classmethod
    def from_env(cls, **overrides: Any) -> Runtime:
        """Build a Runtime whose store comes from ``$LOOM_STORE``.

        Lets the same workflow code run against memory in tests, SQLite on a
        laptop, and Postgres in production without the code knowing which — the
        environment decides, which is where that decision belongs.

            LOOM_STORE=sqlite:///runs.db  python -m my_app

        Defaults to ``memory://`` when unset. Any keyword argument overrides,
        so ``Runtime.from_env(blobs=...)`` still works.

        An agent backend is configured the same way, from whichever provider key
        the environment holds, so a workflow containing ``ctx.agent()`` runs
        here rather than failing on a Runtime that cannot call a model. Pass
        ``agent_backend=`` to choose one explicitly, or ``agent_backend=None``
        to insist on having none.
        """
        from loom.blobs.blob import blob_service_from_env
        from loom.stores.factory import store_from_env

        overrides.setdefault("store", store_from_env())
        if "blobs" not in overrides:
            blobs = blob_service_from_env()
            if blobs is not None:
                overrides["blobs"] = blobs
        if "agent_backend" not in overrides:
            backend = _backend_from_env()
            if backend is not None:
                overrides["agent_backend"] = backend
        return cls(**overrides)

    # -- registration -----------------------------------------------------------------

    def register(
        self, definition: WorkflowDefinition[Any, Any, Any]
    ) -> WorkflowDefinition[Any, Any, Any]:
        existing = self._workflows.get(definition.name)
        if existing is not None and existing is not definition:
            raise ConfigurationError(
                f"a different workflow named '{definition.name}' is registered"
            )
        self._check_grants(definition)
        self._workflows[definition.name] = definition
        return definition

    def _check_grants(self, definition: WorkflowDefinition[Any, Any, Any]) -> None:
        """Refuse a grant that names nothing this Runtime can see.

        Registration is the only moment that knows the effective registry —
        ``rt.toolsets`` chains to the process-global one, so the answer differs
        per Runtime and cannot be settled at decoration. It is also early
        enough to be useful: a typo caught here is a startup failure with a
        suggestion, where the same typo caught at ``ctx.agent()`` is an agent
        reporting that it could not find a tool, hours later.
        """
        grants = getattr(definition, "grants", None)
        if grants is None or not getattr(grants, "toolsets", None):
            return
        issues = grants.validate_against(toolsets=self.toolsets)
        if not issues:
            return
        listed = "; ".join(str(issue) for issue in issues)
        raise ConfigurationError(
            f"workflow '{definition.name}' declares grants that name nothing "
            f"registered: {listed}. A grant entry that matches nothing permits "
            f"nothing, so the workflow would run with an empty toolset rather "
            f"than the one it appears to declare."
        )

    def register_all(self, definitions: Sequence[WorkflowDefinition[Any, Any, Any]]) -> None:
        for definition in definitions:
            self.register(definition)

    async def publish(
        self, target: WorkflowDefinition[Any, Any, Any] | str, **metadata: Any
    ) -> WorkflowRecord:
        """Record a workflow in the durable catalog, and register it here.

        Publishing is explicit rather than a side effect of ``@workflow``, so
        importing a module never writes to storage. What is stored is the catalog
        entry — name, version, code hash, source path — never the code: the file
        on disk stays the single source of truth.
        """
        from loom.runtime.registry import record_for

        definition = self.resolve_workflow(target)
        record = record_for(definition, published_by=self.node_id)
        record.metadata.update(metadata)
        await self.catalog.put(record)
        logger.info("published %s (code_hash=%s)", record.key, record.code_hash[:12])
        return record

    async def published(self) -> list[WorkflowRecord]:
        """Every workflow in the durable catalog, whether or not this process
        imported it. Compare ``record.name in self.workflows`` to tell what this
        Runtime can actually execute."""
        return await self.catalog.list()

    async def provenance(self, run_id: str) -> WorkflowRecord | None:
        """The catalog entry whose code produced *run_id*, if one was published.

        Matches on ``code_hash``, so a run made by code that has since changed
        resolves to the version it actually ran — which is the whole point of
        recording the hash.
        """
        record = await self.store.get_execution(run_id)
        if record is None:
            return None
        for candidate in await self.catalog.list():
            if (
                candidate.name == record.workflow
                and candidate.code_hash
                and candidate.code_hash == record.code_hash
            ):
                return candidate
        return None

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
        credentials: dict[str, str] | Any | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """Start a workflow and drive it until it finishes or parks.

        Raises :class:`AuthorizationError` when a ``role`` is configured without
        ``flow:run``, and :class:`AdmissionRejected` when the workflow's
        ``flow_control`` policy declines to admit this run.

        ``credentials`` is a name→token map or a :class:`CredentialStore`,
        layered over :attr:`credentials` for this run only. ``env`` is a
        name→value map layered over :attr:`env` and ``os.environ``.
        """
        self._authorize(Permission.FLOW_RUN)
        definition = self.resolve_workflow(target)
        await self._admit(definition, metadata)

        if idempotency_key:
            existing = await self.store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                logger.info(
                    "idempotency hit for %s, returning run %s",
                    idempotency_key,
                    existing.run_id,
                )
                return await self._result_for(existing)

        record = await self._open_execution(
            definition,
            input,
            trigger=trigger,
            idempotency_key=idempotency_key,
            parent_run_id=parent_run_id,
            root_run_id=root_run_id,
            tags=tags,
            metadata=metadata,
            run_id=run_id,
            credentials=credentials,
            env=env,
        )
        return await self._drive(record.run_id, deps=deps)

    async def submit(
        self,
        target: WorkflowDefinition[Any, Any, Any] | str,
        input: Any = None,
        **kwargs: Any,
    ) -> str:
        """Start a workflow in the background and return its run id immediately."""
        self._authorize(Permission.FLOW_RUN)
        definition = self.resolve_workflow(target)

        # Check idempotency before admission: a redelivery is not a new arrival,
        # so it should neither consume a rate-limit slot nor be debounced away.
        idempotency_key = kwargs.pop("idempotency_key", None)
        if idempotency_key:
            existing = await self.store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                logger.info(
                    "idempotency hit for %s, returning run %s",
                    idempotency_key,
                    existing.run_id,
                )
                return existing.run_id

        await self._admit(definition, kwargs.get("metadata"))
        credentials = kwargs.pop("credentials", None)
        env = kwargs.pop("env", None)
        deps = kwargs.pop("deps", None)
        record = await self._open_execution(
            definition,
            input,
            trigger=kwargs.pop("trigger", TriggerKind.MANUAL),
            idempotency_key=idempotency_key,
            parent_run_id=kwargs.pop("parent_run_id", None),
            root_run_id=kwargs.pop("root_run_id", None),
            tags=kwargs.pop("tags", ()),
            metadata=kwargs.pop("metadata", None),
            run_id=kwargs.pop("run_id", None),
            credentials=credentials,
            env=env,
        )
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
        # Only a hard failure is truncated. An exhausted entry is re-executed
        # by replay on its own, so pruning it would throw away the attempt
        # history that tells an operator this has failed six times against the
        # same gateway — which is the whole reason the two are distinguished.
        hard = [e for e in journal.entries() if e.status is EntryStatus.FAILED]
        if hard:
            first_failed = hard[0].path
            await self.store.truncate_journal(run_id, first_failed)
            logger.info("retrying %s from %s (%s)", run_id, first_failed, hard[0].name)

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
        self._authorize(Permission.RUN_REPLAY)
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
        source_store = self._run_credentials.get(run_id)
        if source_store is not None:
            self._run_credentials[clone.run_id] = source_store
        return await self._drive(clone.run_id, deps=deps)

    async def cancel(self, run_id: str, *, reason: str = "cancelled by request") -> None:
        """Request cancellation. Takes effect at the next durable operation."""
        self._authorize(Permission.FLOW_CANCEL)
        self._cancelled.add(run_id)
        record = await self.store.get_execution(run_id)
        if record is not None and not record.status.is_terminal:
            record.status = ExecutionStatus.CANCELLED
            record.error = ErrorInfo(type="WorkflowCancelled", message=reason, retryable=False)
            record.finished_at = self.clock.now()
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
        self._authorize(Permission.RUN_VIEW)
        return await self.store.get_execution(run_id)

    async def result(self, run_id: str) -> ExecutionResult:
        self._authorize(Permission.RUN_VIEW)
        return await self._result_for(await self._require(run_id))

    async def list_runs(self, **filters: Any) -> list[ExecutionRecord]:
        self._authorize(Permission.RUN_VIEW)
        return await self.store.list_executions(**filters)

    async def history(self, run_id: str) -> list[Any]:
        self._authorize(Permission.RUN_VIEW)
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
        due = await self.store.due_runs(now or self.clock.now(), limit=limit)
        resumed: list[str] = []
        for run_id in due:
            if run_id in self._driving:
                continue
            resumed.append(run_id)
            self._spawn(self._drive(run_id))
        return resumed

    async def start_scheduler(
        self,
        *,
        interval: Duration = 1.0,
        elector: LeaderElector | None = None,
        group: str = "scheduler",
        lease: Duration = 30.0,
    ) -> None:
        """Run the timer scanner in the background until :meth:`shutdown`.

        Pass an *elector* to run many processes against one store: each tick
        first tries to take the lease for *group*, and only the holder scans for
        due runs. Without it every process would resume the same timers.
        """
        if self._scheduler_task is not None:
            return

        lease_seconds = to_seconds(lease)

        async def loop() -> None:
            while True:
                try:
                    if elector is None or await elector.acquire_leadership(
                        group, lease_seconds
                    ):
                        await self.tick()
                        await self.reclaim_orphans()
                except Exception:
                    logger.exception("scheduler tick failed")
                await self.clock.sleep(to_seconds(interval))

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
        backend = getattr(self.blobs, "backend", None)
        close = getattr(backend, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

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

    async def _open_execution(
        self,
        definition: WorkflowDefinition[Any, Any, Any],
        input: Any,
        *,
        trigger: TriggerKind,
        idempotency_key: str | None,
        parent_run_id: str | None,
        root_run_id: str | None,
        tags: Sequence[str],
        metadata: dict[str, Any] | None,
        run_id: str | None,
        credentials: Any,
        env: dict[str, str] | None,
    ) -> ExecutionRecord:
        """Create the record, stamp env/credential names, bind per-run store.

        The shape gate runs first, before anything is written. A payload the
        workflow cannot accept is not a run that failed — it is a run that never
        started, and recording it as the former would put work that could never
        happen into every reliability number and leave something in history that
        ``retry()`` fails identically forever.
        """
        self._check_input(definition, input)
        meta = _sanitize_metadata(metadata)
        if env:
            from loom.runtime.environment import validate_run_env

            meta["loom.env"] = validate_run_env(env)
        per_run = await _as_credential_store(credentials)
        if per_run is not None:
            meta["loom.credential_names"] = list(await per_run.names())
        record = ExecutionRecord(
            workflow=definition.name,
            workflow_version=definition.version,
            status=ExecutionStatus.PENDING,
            trigger=trigger,
            input=encode(input),
            parent_run_id=parent_run_id,
            root_run_id=root_run_id or parent_run_id,
            idempotency_key=idempotency_key,
            code_hash=definition.code_hash,
            created_at=self.clock.now(),
            tags=list(tags),
            metadata=meta,
        )
        if run_id:
            record.run_id = run_id
        await self.store.create_execution(record)
        if per_run is not None:
            self._run_credentials[record.run_id] = per_run
        return record

    def _check_input(
        self, definition: WorkflowDefinition[Any, Any, Any], input: Any
    ) -> None:
        """Refuse a payload the workflow's declared input cannot accept.

        Shallow by design — see :mod:`loom.runtime.validation`. A
        workflow that declares nothing checkable is run as before.
        """
        if not self.validate_input:
            return

        from loom.runtime.validation import shape_error

        schema = definition.input_schema()
        mismatch = shape_error(schema, input)
        if mismatch is None:
            return
        declared = (schema or {}).get("title") or definition.input_name
        raise InputMismatch(
            f"{mismatch.message} Workflow '{definition.name}' expects "
            f"{definition.input_name}: {declared}.",
            workflow=definition.name,
            path=mismatch.path,
        )

    async def _credentials_for(self, record: ExecutionRecord) -> Any:
        """Layer per-run, resolver, and Runtime stores for this record.

        Per-run and resolver stores are the run's identity. Runtime-level
        credentials are ambient fallback and must not satisfy a name this
        run declared — that would swap the caller-supplied principal for
        whatever happens to be in the environment.
        """
        from loom.connectors.credentials import LayeredCredentialStore

        identity: list[Any] = []
        per_run = self._run_credentials.get(record.run_id)
        if per_run is not None:
            identity.append(per_run)
        if self.credential_resolver is not None:
            import inspect

            resolved = self.credential_resolver(record)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if resolved is not None:
                identity.append(resolved)
        ambient: list[Any] = [self.credentials] if self.credentials is not None else []
        required = frozenset(record.metadata.get("loom.credential_names") or ())
        if not identity and not ambient and not required:
            return None
        if len(identity) == 1 and not ambient and not required:
            return identity[0]
        if not identity and len(ambient) == 1 and not required:
            return ambient[0]
        return LayeredCredentialStore(*identity, ambient=ambient, required=required)

    async def persist_journal(self, record: ExecutionRecord, journal: Journal) -> None:
        dirty = journal.drain_dirty()
        if dirty:
            await self.store.save_journal(record.run_id, dirty)

    # -- the core loop ----------------------------------------------------------------

    async def _drive(self, run_id: str, *, deps: Any = None) -> ExecutionResult:
        if run_id in self._driving:
            return await self.wait(run_id, timeout=self.max_inline_wait or 30)
        self._driving.add(run_id)
        heartbeat = asyncio.ensure_future(self._heartbeat(run_id))
        try:
            return await self._drive_inner(run_id, deps=deps)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            self._driving.discard(run_id)
            await self._release_lease(run_id)

    async def _drive_inner(self, run_id: str, *, deps: Any) -> ExecutionResult:
        record = await self._require(run_id)
        definition = self.resolve_workflow(record.workflow)

        while True:
            if record.status.is_terminal:
                return await self._result_for(record)
            if run_id in self._cancelled:
                return await self._finish_cancelled(record)

            journal = Journal(
                await self.store.load_journal(run_id),
                compatibility=self.compatibility,
                verify=self.verify,
                # A replay rehearses what happened, so a step that gave up must
                # give up again. Everything else — retry, resume, an outer
                # driver re-enqueueing — wants it attempted, with the work
                # before it still served from the journal.
                resume_exhausted=record.trigger is not TriggerKind.REPLAY,
            )
            record.status = ExecutionStatus.RUNNING
            record.attempt += 1
            record.started_at = record.started_at or self.clock.now()
            record.wake_at = None
            record.awaiting_event = None
            record.lease_owner = self.node_id
            record.lease_expires_at = self.clock.now() + timedelta(seconds=self.lease_ttl)
            await self.store.update_execution(record)

            ctx = Context(
                runtime=self,
                record=record,
                journal=journal,
                definition=definition,
                deps=deps if deps is not None else self.deps,
                credentials=await self._credentials_for(record),
                env=_environment_for(record, self.env),
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
            except AuthExpired as expired:
                # A toolset's CredentialStore lookup found an expired
                # credential it cannot renew unattended — park exactly as a
                # raised Suspend would, on an event named after the
                # credential, so 'loom connect <name>' plus that event
                # delivery is what resumes it (never a bare retry loop; see
                # AuthExpired's presence in core/retry.py::PERMANENT_ERRORS).
                suspension = Suspend(
                    str(expired),
                    path="",
                    awaiting_event=f"credential:{expired.name}" if expired.name else None,
                )
                await self.persist_journal(record, journal)
                should_continue = await self._park(record, suspension, journal)
                if should_continue:
                    continue
                span.set_status("suspended")
                span.end()
                return await self._result_for(record, journal)
            except ContinueAsNew as rotation:
                span.set_status("ok")
                span.end()
                await self.persist_journal(record, journal)
                return await self._finish_rotated(
                    record, journal, rotation, definition, deps=deps
                )
            except WorkflowCancelled:
                span.set_status("cancelled")
                span.end()
                unwound = await ctx.run_compensations()
                await self.persist_journal(record, journal)
                return await self._finish_cancelled(record, journal, unwound)
            except Exception as error:
                span.record_exception(error)
                span.end()
                # Unwind the saga before the record goes terminal, so an operator
                # reading a FAILED run already sees the rollback outcome.
                unwound = await ctx.run_compensations()
                await self.persist_journal(record, journal)
                return await self._finish_failed(record, journal, error, definition, unwound)
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
                remaining = (suspension.wake_at - self.clock.now()).total_seconds()
                budget = max(budget, min(remaining, self.max_inline_wait))
            if budget <= 0:
                return False
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(waiter.wait(), budget)
            return True

        if suspension.wake_at is not None:
            remaining = (suspension.wake_at - self.clock.now()).total_seconds()
            if remaining <= self.max_inline_wait:
                await self.clock.sleep(max(0.0, remaining))
                return True
        return False

    async def _finish_completed(
        self, record: ExecutionRecord, journal: Journal, output: Any
    ) -> ExecutionResult:
        record.status = ExecutionStatus.COMPLETED
        record.output = encode(output)
        record.finished_at = self.clock.now()
        record.usage = journal.total_usage()
        await self.store.update_execution(record)
        self._release_admission(record)
        self._signal_completion(record.run_id)
        return await self._result_for(record, journal, raw_output=output)

    async def _finish_rotated(
        self,
        record: ExecutionRecord,
        journal: Journal,
        rotation: ContinueAsNew,
        definition: WorkflowDefinition[Any, Any, Any],
        *,
        deps: Any,
    ) -> ExecutionResult:
        """Close out a rotating run and start its successor from the seed.

        The successor is a fresh execution with an empty journal — that is the
        whole point of ``continue_as_new``: a forever-flow that would otherwise
        accumulate an unbounded journal gets a clean one, while ``root_run_id``
        keeps the whole chain queryable as a single logical flow.
        """
        root = record.root_run_id or record.run_id
        successor = ExecutionRecord(
            workflow=definition.name,
            workflow_version=definition.version,
            status=ExecutionStatus.PENDING,
            trigger=record.trigger,
            input=encode(rotation.seed),
            parent_run_id=record.run_id,
            root_run_id=root,
            created_at=self.clock.now(),
            tags=list(record.tags),
            metadata=dict(record.metadata),
        )
        await self.store.create_execution(successor)

        record.status = ExecutionStatus.COMPLETED
        record.root_run_id = root
        record.finished_at = self.clock.now()
        record.usage = journal.total_usage()
        record.metadata["continued_as"] = successor.run_id
        await self.store.update_execution(record)
        self._release_admission(record)
        self._signal_completion(record.run_id)
        logger.info("run %s rotated into %s", record.run_id, successor.run_id)

        parent_store = self._run_credentials.get(record.run_id)
        if parent_store is not None:
            self._run_credentials[successor.run_id] = parent_store

        # Background, not inline: a forever-flow rotating inline would recurse
        # until the stack ran out.
        self._spawn(self._drive(successor.run_id, deps=deps))
        return await self._result_for(record, journal)

    async def _finish_failed(
        self,
        record: ExecutionRecord,
        journal: Journal,
        error: BaseException,
        definition: WorkflowDefinition[Any, Any, Any],
        compensation_failures: Sequence[str] = (),
    ) -> ExecutionResult:
        record.status = ExecutionStatus.FAILED
        record.error = ErrorInfo.from_exception(error)
        record.finished_at = self.clock.now()
        record.usage = journal.total_usage()
        if compensation_failures:
            record.metadata["compensation_failures"] = list(compensation_failures)
        await self.store.update_execution(record)
        self._release_admission(record)
        self._signal_completion(record.run_id)
        logger.warning("run %s failed: %s", record.run_id, error)

        await self._dispatch_failure_handlers(record, definition)
        return await self._result_for(record, journal)

    async def _finish_cancelled(
        self,
        record: ExecutionRecord,
        journal: Journal | None = None,
        compensation_failures: Sequence[str] = (),
    ) -> ExecutionResult:
        record.status = ExecutionStatus.CANCELLED
        record.finished_at = self.clock.now()
        if compensation_failures:
            record.metadata["compensation_failures"] = list(compensation_failures)
        await self.store.update_execution(record)
        self._release_admission(record)
        self._signal_completion(record.run_id)
        return await self._result_for(record, journal)

    async def _dispatch_failure_handlers(
        self, record: ExecutionRecord, definition: WorkflowDefinition[Any, Any, Any]
    ) -> None:
        """Invoke the workflow's own handler plus any registered ``OnFailure`` workflows."""
        from loom.triggers.specs import OnFailure

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

    def require_artifacts(self) -> Any:
        """The artifact service, or a clear error explaining how to get one."""
        if self.artifacts is None:
            raise ConfigurationError(
                "artifacts need blob storage. Pass blobs=BlobService(...) to Runtime(), "
                "or artifacts=ArtifactService(...) to supply your own."
            )
        return self.artifacts

    def require_staging(self) -> Any:
        """The staging manager, or a clear error explaining how to get one."""
        if self.staging is None:
            raise ConfigurationError(
                "staging needs blob storage. Pass blobs=BlobService(...) to Runtime()."
            )
        return self.staging

    def require_signed_urls(self) -> Any:
        """The signed-URL service, or a clear error explaining how to get one."""
        if self.signed_urls is None:
            raise ConfigurationError(
                "signed URLs need blob storage. Pass blobs=BlobService(...) to Runtime()."
            )
        return self.signed_urls

    async def _heartbeat(self, run_id: str) -> None:
        """Extend this run's lease while we are actually working on it.

        Without it a long step would let the lease expire and another node would
        reclaim a run that is progressing fine. Renewing at a third of the TTL
        leaves room for two missed beats before anyone considers us dead.
        """
        interval = max(1.0, self.lease_ttl / 3)
        while True:
            # Deliberately the real clock, not self.clock. A lease is a claim
            # against other processes, which are running on wall time whatever
            # this one believes; and a ManualClock's sleep returns immediately,
            # so heartbeating on it would spin.
            await asyncio.sleep(interval)
            record = await self.store.get_execution(run_id)
            if record is None or record.status.is_terminal:
                return
            if record.lease_owner != self.node_id:
                # Someone else took over; stop touching their run.
                return
            record.lease_expires_at = self.clock.now() + timedelta(seconds=self.lease_ttl)
            await self.store.update_execution(record)

    async def _release_lease(self, run_id: str) -> None:
        """Drop our claim so the run is not mistaken for an orphan we abandoned."""
        record = await self.store.get_execution(run_id)
        if record is None or record.lease_owner != self.node_id:
            return
        record.lease_owner = None
        record.lease_expires_at = None
        await self.store.update_execution(record)

    async def reclaim_orphans(
        self, now: datetime | None = None, *, limit: int = 100
    ) -> list[str]:
        """Resume runs left ``RUNNING`` by a node that died.

        A crashed worker leaves its run marked RUNNING forever — no timer covers
        it, because it is not waiting for one. This finds records whose lease has
        expired and re-drives them; the journal makes that safe, since everything
        already completed is served from it rather than repeated.

        Call from a scheduler loop. Returns the run ids picked up.
        """
        moment = now or self.clock.now()
        stale = [
            record
            for record in await self.store.list_executions(
                status=ExecutionStatus.RUNNING, limit=limit
            )
            if record.run_id not in self._driving
            and record.lease_expires_at is not None
            and _as_utc(record.lease_expires_at) <= moment
        ]

        reclaimed: list[str] = []
        for record in stale:
            logger.warning(
                "reclaiming run %s orphaned by node %s",
                record.run_id,
                record.lease_owner,
            )
            reclaimed.append(record.run_id)
            self._spawn(self._drive(record.run_id))
        return reclaimed

    def _release_admission(self, record: ExecutionRecord) -> None:
        """Free the in-flight slot a concurrency or singleton policy reserved.

        Called only from terminal transitions — a suspended run still occupies
        its slot, which is what makes ``singleton`` mean "one live run" rather
        than "one running instruction".
        """
        if self.admission is None:
            return
        definition = self._workflows.get(record.workflow)
        if definition is None or definition.flow_control is None:
            return
        self.admission.record_end(
            record.workflow, str(record.metadata.get("partition_key", ""))
        )

    def _authorize(self, permission: Permission) -> None:
        """Enforce RBAC when a role is configured; a no-op otherwise."""
        if self.role is not None:
            require(self.role, permission)

    async def _admit(
        self,
        definition: WorkflowDefinition[Any, Any, Any],
        metadata: dict[str, Any] | None,
    ) -> None:
        """Apply the workflow's flow-control policy before a run is created.

        Rejecting here rather than after ``create_execution`` is the point: a
        debounced or skipped trigger should leave no run record behind, or the
        run history fills with executions that never did anything.
        """
        policy = definition.flow_control
        if policy is None or self.admission is None:
            return

        partition = str((metadata or {}).get("partition_key", ""))
        outcome = await self.admission.evaluate(
            definition.name, policy, partition_key=partition
        )
        if outcome.decision is AdmissionDecision.ADMIT:
            self.admission.record_start(definition.name, partition)
            return
        raise AdmissionRejected(
            f"'{definition.name}' was not admitted ({outcome.decision.value}): "
            f"{outcome.reason}",
            decision=outcome.decision.value,
            delay_seconds=outcome.delay_seconds,
        )

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
        if record.status.is_terminal:
            self._run_credentials.pop(record.run_id, None)
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


def _backend_from_env() -> Any | None:
    """Build an agent backend from whichever provider key is present.

    ``None`` when no key is set, or when the pieces are not installed — a
    Runtime without an agent backend is a valid Runtime, and most workflows
    never call one.
    """
    import os

    candidates = (
        ("ANTHROPIC_API_KEY", "AnthropicProvider"),
        ("OPENAI_API_KEY", "OpenAIProvider"),
        ("GEMINI_API_KEY", "GeminiProvider"),
    )
    for variable, provider_name in candidates:
        if not os.environ.get(variable):
            continue
        try:
            from loom.agents import providers
            from loom.agents.backend import BuiltInBackend

            provider = getattr(providers, provider_name)()
        except Exception:
            continue
        return BuiltInBackend(model=provider)
    return None


_RESERVED_METADATA = frozenset({"loom.env", "loom.credential_names"})


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Drop reserved keys from caller-supplied metadata.

    ``POST /runs`` accepts arbitrary metadata, so ``loom.env`` /
    ``loom.credential_names`` are injection vectors unless stripped here —
    then we write our own. Other ``loom.*`` keys (``loom.principal``) stay;
    identity pins those.
    """
    out = dict(metadata or {})
    for key in _RESERVED_METADATA:
        out.pop(key, None)
    return out


async def _as_credential_store(credentials: Any) -> Any:
    """Coerce a name→token map or an existing store into a CredentialStore."""
    if credentials is None:
        return None
    if isinstance(credentials, dict):
        from loom.connectors.credentials import (
            MemoryCredentialStore,
            StoredCredential,
        )

        store = MemoryCredentialStore()
        for name, token in credentials.items():
            await store.put(name, StoredCredential(token=Secret(str(token))))
        return store
    return credentials


def _environment_for(record: ExecutionRecord, runtime_env: dict[str, str]) -> Any:
    from loom.runtime.environment import RunEnvironment

    run_env = record.metadata.get("loom.env")
    if not isinstance(run_env, dict):
        run_env = {}
    return RunEnvironment(run_env=run_env, runtime_env=runtime_env)

