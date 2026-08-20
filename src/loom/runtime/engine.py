"""The durable execution engine.

The loop is deliberately small. Load the journal, re-enter the workflow body, and let the
journal short-circuit everything that already happened. The body either returns (done),
raises :class:`Suspend` (park until a timer or event), or raises (failed). Because every
side effect is journaled before it is observed, re-entering is always safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import uuid
import weakref
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from loom.blobs.refcount import record_refs
from loom.core.exceptions import (
    AdmissionRejected,
    AuthExpired,
    ConcurrentUpdateError,
    ConfigurationError,
    ContinueAsNew,
    InputMismatch,
    RegistryError,
    Suspend,
    WorkflowCancelled,
    WorkflowError,
)
from loom.core.models import (
    ErrorInfo,
    Event,
    EventDelivery,
    ExecutionRecord,
    ExecutionResult,
    ExecutionStatus,
    TriggerKind,
)
from loom.core.redaction import DEFAULT_REDACT_KEYS
from loom.core.secret import Secret
from loom.core.serde import decode, encode
from loom.core.types import Duration, to_seconds
from loom.identity.principal import ServicePrincipal
from loom.observability.tracing import NoopTracer, Tracer
from loom.runtime.backend import DurabilityBackend, EmbeddedBackend
from loom.runtime.clock import Clock, SystemClock
from loom.runtime.context import Context
from loom.runtime.effects import DirectBroker, EffectBroker, RunObserver
from loom.runtime.flowcontrol import AdmissionController, AdmissionDecision
from loom.runtime.journal import (
    CompatibilityMode,
    EntryStatus,
    Journal,
    VerifyMode,
)
from loom.runtime.leader import LeaderElector
from loom.runtime.registry import WorkflowRecord
from loom.runtime.sandbox import (
    ExecutionSandbox,
    InlineSandbox,
    RuntimeChannel,
    SandboxBody,
    SandboxPolicy,
    SandboxViolation,
)
from loom.runtime.state import (
    InMemoryRunStream,
    RunStream,
    StateStore,
    StoreBackedState,
)
from loom.runtime.versions import VersionPolicy
from loom.runtime.workflow import WorkflowDefinition
from loom.security.authority import Authority
from loom.security.rbac import Permission, Role, require, requires
from loom.steps.definition import StepDefinition
from loom.stores.base import CacheStore
from loom.stores.memory import MemoryStore

logger = logging.getLogger("workflow.engine")


#: How a body exited, keyed by the exception that carried it out. Parking is
#: not failure and cancellation is not failure, and a body hook is the one place
#: those are distinguishable without re-reading the record afterwards.
_BODY_EXITS = {
    "Suspend": "suspended",
    "AuthExpired": "suspended",
    "ContinueAsNew": "rotated",
    "WorkflowCancelled": "cancelled",
    "CancelledError": "abandoned",
}


#: Statuses meaning "somebody should still be working on this run". A record
#: left in one of these with an expired lease is nobody's, and only
#: ``reclaim_orphans`` covers it — no timer is watching, because it is not
#: waiting for one.
_UNFINISHED = (ExecutionStatus.PENDING, ExecutionStatus.RUNNING)

#: Rows per store round trip while scanning for expired leases. Bigger than the
#: number of orphans anyone expects, small enough that one page is a cheap read.
_RECLAIM_PAGE = 500


#: Every optional port a Runtime can be given, by attribute name.
#:
#: One list, so "what can this deployment do" has a single answer. It was
#: previously answerable only by reading a fifty-one parameter constructor and
#: a string map in ``loom.nodes.base``, which is why a node could demand a
#: capability nothing had told the operator was missing.
CAPABILITY_PORTS: tuple[str, ...] = (
    "blobs",
    "artifacts",
    "staging",
    "signed_urls",
    "human",
    "connections",
    "credentials",
    "embeddings",
    "vectors",
    "events",
    "checkpoints",
    "sources",
    "sessions",
    "spill",
    "versions",
    "catalog",
    "admission",
    "agent_backend",
)


def _resume_event(run_id: str) -> str:
    """The event a paused run waits on. Per-run, so releasing one holds nothing else."""
    return f"resume:{run_id}"


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
        versions: Any | None = None,
        events: Any | None = None,
        checkpoints: Any | None = None,
        sources: Any | None = None,
        node_id: str | None = None,
        lease_ttl: Duration = 60.0,
        journal_warn_entries: int = 5_000,
        journal_max_entries: int = 50_000,
        journal_warn_payload_bytes: int = 1_048_576,
        journal_max_payload_bytes: int = 8_388_608,
        inline_timer_threshold: Duration = 2.0,
        max_inline_wait: Duration = 0.0,
        flush_every: int = 1,
        compatibility: CompatibilityMode = CompatibilityMode.STRICT,
        verify: VerifyMode = VerifyMode.STRICT,
        validate_input: bool = True,
        connections: Any | None = None,
        embeddings: Any | None = None,
        vectors: Any | None = None,
        redact_keys: Iterable[str] | None = None,
        spill: Any | None = None,
        strict_determinism: bool = False,
        version_policy: VersionPolicy = VersionPolicy.LATEST,
        sandbox: ExecutionSandbox | None = None,
        sandbox_policy: SandboxPolicy | None = None,
        sandbox_steps: Mapping[str, Any] | None = None,
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
        # Falls back to the store, and all four shipped stores implement
        # CacheStore as well as ExecutionStore — but the ExecutionStore
        # protocol does not promise it, so the fallback is the one place
        # that has to assert the arrangement the store table documents.
        self.cache: CacheStore = cache if cache is not None else cast("CacheStore", self.store)
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

        self.events = events
        """Optional :class:`~loom.events.log.EventLog`. Off by default: a
        Runtime that nobody feeds events has no reason to hold a log, and
        wiring one implicitly would put a topic's worth of keys in every
        store. ``StoreBackedEventLog(rt.store)`` needs no infrastructure
        beyond the store already here; a host with Kafka or Redis Streams
        passes its own adapter and proves it with
        :func:`loom.testing.conformance.verify_event_log`."""
        self.checkpoints = checkpoints
        """Optional :class:`~loom.events.log.Checkpoints` — where each
        subscriber has read to. Defaults, when an ``EventDispatcher`` needs
        one, to the store-backed implementation; separate from ``events``
        because a host may well keep the log in Kafka and the cursors in its
        own database."""

        if sources is not None:
            self.sources = sources
        else:
            from loom.events.source_registry import (
                EventSourceRegistry,
                get_source_catalog,
            )

            # Chains to the process-global registry, exactly as `toolsets` and
            # `nodes` do: a `loom_event_source` entry point reaches every
            # Runtime, while rt.sources.register(...) stays local to this one.
            self.sources = EventSourceRegistry(parent=get_source_catalog())

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

        self.sandbox: ExecutionSandbox = sandbox or InlineSandbox()
        """Where a workflow body is invoked. The default runs it in this
        process, which is what a developer wants and what every existing
        Runtime already did; a host executing code a model wrote against
        credentials the host holds passes ``SubprocessSandbox``.

        Deliberately not a :class:`DurabilityBackend`: that port answers where
        durability *lives*, and isolation is orthogonal to it — you want a
        sandbox on the embedded backend, and Temporal has its own workers."""

        self.sandbox_policy: SandboxPolicy = sandbox_policy or SandboxPolicy()
        """What a sandboxed body may reach. Ignored by ``InlineSandbox``, which
        enforces nothing and says so through ``enforces``."""

        self._sandbox_steps_extra: Mapping[str, Any] = dict(sandbox_steps or {})
        """Steps a sandboxed body can call that are not reachable by scanning
        the workflow function's own globals — the case for a step a host
        bridges in dynamically (one ``@step`` fronting many operations, since
        the operations are not known until a task resolves its tools) rather
        than importing by name into the workflow module. Merged into, and
        overridden by, whatever :meth:`_sandbox_steps` finds by scanning."""

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

        if versions is not None:
            self.versions = versions
        else:
            from loom.runtime.versions import StoreBackedVersionStore

            self.versions = StoreBackedVersionStore(self.store, self.blobs)
        """Immutable workflow versions. Nothing is written unless :meth:`publish`
        is given ``source=``, so a Runtime that never versions anything pays
        nothing. A host with its own version storage — graph database, object
        store, anything — passes ``versions=`` and implements the protocol."""

        self.node_id = node_id or f"node-{uuid.uuid4().hex[:12]}"
        """Identifies this process in run leases, so an orphaned run can be told
        apart from one another node is actively working on."""
        self.lease_ttl = to_seconds(lease_ttl)
        """How long a run lease stays valid without a heartbeat."""

        self.journal_warn_entries = journal_warn_entries
        """Log once when a run's journal passes this. A long-lived flow that never
        rotates degrades slowly and silently; this makes it visible early."""
        self.journal_warn_payload_bytes = journal_warn_payload_bytes
        """Log once when a single journal payload passes this, with no blob
        service configured. 1 MiB — comfortably above any ordinary step result
        and well under every backend's ceiling."""
        self.journal_max_payload_bytes = journal_max_payload_bytes
        """Fail the run when one journal payload passes this. Set to 0 to disable.

        The entry budget below counts *entries*, so a single 200 MB step output
        is `1` to it and passes unnoticed — then lands as an opaque
        `DocumentTooLarge` on Mongo (16 MB BSON ceiling) or as a silent
        multi-hundred-megabyte row on SQLite and Postgres that is re-read and
        re-parsed on every replay thereafter. 8 MiB leaves room under Mongo's
        limit for the rest of the document.

        Only consulted when `blobs` is None. With a `BlobService` a payload over
        its threshold becomes a `blob:` reference and never reaches this."""
        self.journal_max_entries = journal_max_entries
        """Fail the run when its journal passes this. Set to 0 to disable.

        Failing loudly beats replaying a million entries on every attempt until
        the process runs out of memory."""

        self.version_policy = version_policy
        """What an in-flight run resumes against when the code has changed.

        A run records the ``code_hash`` of the definition that started it, and
        until this existed nothing ever compared it: ``resolve_workflow`` reads
        the in-process registry and only the registry, so a run parked on a
        24-hour approval resumed against whatever was deployed in the meantime.
        The version store — ``WorkflowVersion``, ``content_hash``,
        ``activate_version`` — was consulted only to recover source text for a
        sandbox.

        Defaults to :attr:`VersionPolicy.LATEST`, which is what every release
        before this did, because the alternative punishes the common case: a
        ``code_hash`` moves on any edit, comments included, and refusing every
        in-flight run after a no-op redeploy is worse than the hazard. What
        makes ``LATEST`` defensible now is that the two ways a changed body can
        actually corrupt a replay — a different operation at a position, and
        the same operation with different arguments — are both refused by
        default (``CompatibilityMode.STRICT``, ``VerifyMode.STRICT``).

        Set :attr:`VersionPolicy.REFUSE` where a divergence must never be
        attempted, or :attr:`VersionPolicy.PINNED` to resume against the source
        the run started with.
        """

        self.compatibility = compatibility
        self.verify = verify
        """Whether a replayed entry must prove it belongs to the call that found it.

        Defaults to :attr:`VerifyMode.STRICT`: a mismatch raises rather than
        serving another call site's recorded answer. It was ``WARN``, and the
        difference it was tolerating was mostly the engine's own — durable
        calls under ``ctx.gather`` took their journal paths from a shared
        counter as they ran, so replaying with different timings served each
        branch the other's values and reported ``completed``. With branch-local
        numbering that cause is gone, and what is left is a real divergence.

        A workflow that deliberately derives a step's arguments from
        ``ctx.state`` — not journaled, by design — should set
        :attr:`VerifyMode.WARN` and say so.
        """
        self.connections = connections
        """A :class:`~loom.toolsets.connections.ConnectionBroker`, or ``None``.

        Where a named credential is exchanged for a token. ``io.http_request``
        reads it so a workflow can call a service LOOM has no toolset for
        without putting the token in the node's payload — which is journaled.
        Naming a credential is safe to record; holding one is not."""
        self.embeddings = embeddings
        """An :class:`~loom.knowledge.EmbeddingProvider`, or ``None``.

        What turns text into vectors for the ``knowledge.*`` nodes. ``None``
        means nothing can be indexed or searched, and the nodes say so rather
        than failing as an AttributeError from inside a body."""
        self.vectors = vectors
        """A :class:`~loom.knowledge.VectorStore`, or ``None``.

        ``StoreBackedVectorStore(store)`` needs no service beyond the one
        already backing the journal. A host with a million vectors implements
        the port over pgvector, Pinecone, or Qdrant — which is what it is
        for."""
        self.redact_keys = (
            DEFAULT_REDACT_KEYS if redact_keys is None else frozenset(redact_keys)
        )
        """Argument and field names replaced with ``***`` in recorded step inputs.

        Step inputs are written for a person reading a trace and are never
        replayed, so removing a credential from one costs a debugging aid and
        nothing else. Pass a wider set to add house names; pass an empty one to
        record inputs verbatim, as every version before this did.

        This decides on *names*. A value typed :data:`loom.Secret` redacts
        itself wherever it is written and is the stronger guarantee — see
        :mod:`loom.core.secrets`."""
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
        if strict_determinism:
            # Refused rather than accepted and ignored — the rule
            # `SubprocessSandbox` already applies to a limit it cannot honour.
            # This parameter was assigned here and read at zero sites in the
            # codebase for its whole life, while the documentation said
            # "violating these raises NondeterminismError in strict mode". A
            # host that set it believed it had a runtime guard and had nothing.
            raise ConfigurationError(
                "Runtime(strict_determinism=True) never enforced anything — it "
                "was stored and never read. Determinism is checked three other "
                "ways, all of which do work: the static scan that runs at "
                "@workflow declaration time (loom.runtime.determinism), "
                "CodeValidator for generated code, and "
                "loom.testing.assert_replays() for a workflow you have "
                "written. For a hard runtime guard in a test, use "
                "loom.runtime.determinism.strict_determinism() as a context "
                "manager."
            )

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
        self._paused: set[str] = set()
        self._driving: set[str] = set()
        self._background: set[asyncio.Task[Any]] = set()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._services: weakref.WeakSet[Any] = weakref.WeakSet()

        from loom.runtime.hooks import HookRegistry

        self.hooks = HookRegistry(self)
        """Middleware around every durable operation.

        Empty by default and free while it stays that way: the first
        registration is what puts a ``HookBroker`` in the broker chain, so a
        Runtime with no hooks runs exactly the chain it ran before this existed.

            @rt.hooks.before_tool(effect=EffectClass.DESTRUCTIVE)
            async def confirm(ctx): ctx.ask("deletes data")

        Per-Runtime and deliberately not chained to a process-global registry —
        see :class:`~loom.runtime.hooks.HookRegistry`.
        """

    @classmethod
    def from_env(cls, **overrides: Any) -> Runtime:
        """Build a Runtime whose store comes from ``$LOOM_STORE``.

        Lets the same workflow code run against memory in tests, SQLite on a
        laptop, and Postgres in production without the code knowing which — the
        environment decides, which is where that decision belongs.

            LOOM_STORE=sqlite://runs.db  python -m my_app

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

    @requires(Permission.FLOW_DEPLOY)
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
        source = metadata.pop("source", None)
        pins = metadata.pop("pins", None)
        record.metadata.update(metadata)

        if source is not None:
            # Committing the source alongside the catalog entry is what lets a
            # run be replayed against the code it actually ran, rather than
            # whatever is on disk now. Optional: the file stays the source of
            # truth for a host that does not want a second copy.
            from loom.runtime.versions import Pins, WorkflowVersion

            committed = await self.versions.commit(
                WorkflowVersion(
                    workflow=record.name,
                    source=source,
                    # The fingerprint a finished run carries, so version_of()
                    # can tie one to the other. Without it the two hash spaces
                    # never meet and every run resolves to no version.
                    code_hash=record.code_hash,
                    pins=pins or Pins(),
                    created_by=self.node_id,
                )
            )
            record.metadata["version_number"] = committed.version
            record.metadata["content_hash"] = committed.content_hash

        await self.catalog.put(record)
        logger.info("published %s (code_hash=%s)", record.key, record.code_hash[:12])
        return record

    async def published(self) -> list[WorkflowRecord]:
        """Every workflow in the durable catalog, whether or not this process
        imported it. Compare ``record.name in self.workflows`` to tell what this
        Runtime can actually execute."""
        published: list[WorkflowRecord] = await self.catalog.list()
        return published

    async def version_of(self, run_id: str) -> Any:
        """The :class:`WorkflowVersion` whose source produced *run_id*.

        Matches on ``code_hash``, the same tie ``provenance`` uses — so a run
        made by code that has since changed resolves to the version it actually
        ran, which is the whole point of recording the hash. ``None`` when the
        workflow was published without source.
        """
        record = await self.store.get_execution(run_id)
        if record is None or not record.code_hash:
            return None
        return await self.versions.resolve(record.workflow, record.code_hash)

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
                found: WorkflowRecord = candidate
                return found
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

    @requires(Permission.FLOW_RUN)
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

    @requires(Permission.FLOW_RUN)
    async def submit(
        self,
        target: WorkflowDefinition[Any, Any, Any] | str,
        input: Any = None,
        **kwargs: Any,
    ) -> str:
        """Start a workflow in the background and return its run id immediately."""
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
        record = await self._open_execution_idempotently(
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

    async def _open_execution_idempotently(
        self, definition: Any, input: Any, **kwargs: Any
    ) -> ExecutionRecord:
        """Create the record, or hand back the one that beat us to the key.

        The check in :meth:`submit` narrows the race; the store's own UNIQUE
        constraint closes it. So the loser of that race finds out by having its
        insert refused — and what it should do is return the winner's run,
        because the caller asked for "a run for this key" and there is one.

        Raising instead would turn a correctly deduplicated scheduled fire into
        an error in the dispatcher, which then declines to advance the trigger
        and tries the same occurrence again on the next tick. The deduplication
        would be working and the schedule would still be stuck.

        Deliberately catching broadly: each backend signals a duplicate in its
        driver's own vocabulary — ``IntegrityError``, ``DuplicateKeyError``,
        ``ValidationError``. Narrowing to a list of those would silently stop
        covering the next backend someone adds. The re-read is what makes it
        safe: nothing is swallowed unless the key really does resolve now, and
        anything else is re-raised untouched.
        """
        key = kwargs.get("idempotency_key")
        try:
            return await self._open_execution(definition, input, **kwargs)
        except Exception:
            if not key:
                raise
            winner = await self.store.find_by_idempotency_key(key)
            if winner is None:
                raise
            logger.info(
                "lost the idempotency race for %s, using run %s",
                key,
                winner.run_id,
            )
            return winner

    @requires(Permission.FLOW_RUN)
    async def resume(self, run_id: str, *, deps: Any = None) -> ExecutionResult:
        """Continue a parked run. Safe to call redundantly."""
        return await self._drive(run_id, deps=deps)

    @requires(Permission.FLOW_RUN)
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

    @requires(Permission.RUN_REPLAY)
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
        # The clone shares every blob ref with its source, which is precisely
        # how compacting one used to destroy the other's payloads. Copying the
        # journal without recording the clone as a referent would leave that
        # sharing invisible to retention.
        copied = await self.store.load_journal(run_id)
        await self.store.save_journal(clone.run_id, copied)
        await record_refs(self.cache, clone.run_id, copied)
        source_store = self._run_credentials.get(run_id)
        if source_store is not None:
            self._run_credentials[clone.run_id] = source_store
        return await self._drive(clone.run_id, deps=deps)

    @requires(Permission.FLOW_CANCEL)
    async def cancel(self, run_id: str, *, reason: str = "cancelled by request") -> None:
        """Request cancellation. Takes effect at the next durable operation."""
        self._cancelled.add(run_id)
        record = await self.store.get_execution(run_id)
        if record is None or record.status.is_terminal:
            return

        record.cancel_requested = True
        if self._is_being_driven(record):
            # Its driver is alive and will see the flag on its next heartbeat.
            # Writing CANCELLED here instead would race that driver — it
            # overwrites the record on its next update — and would skip the
            # compensation stack entirely, because only the process running the
            # body can unwind it.
            await self.store.update_execution(record)
            return

        record.status = ExecutionStatus.CANCELLED
        record.error = ErrorInfo(type="WorkflowCancelled", message=reason, retryable=False)
        record.finished_at = self.clock.now()
        await self.store.update_execution(record)
        self._signal_completion(run_id)

    @requires(Permission.FLOW_CANCEL)
    async def pause(self, run_id: str) -> None:
        """Hold a run at its next durable boundary.

        Gated on ``FLOW_CANCEL`` rather than a permission of its own: pausing
        is the same authority as stopping, applied reversibly, and inventing a
        third token for it would mean a role that can kill a run but not hold
        it.

        Takes effect between durable calls, never inside one — which is what
        keeps the journal consistent and is the same boundary cancellation
        uses. A run already parked on a timer or an event stays parked and will
        not advance when it fires.
        """
        record = await self._require(run_id)
        if record.status.is_terminal:
            raise RegistryError(
                f"run {run_id} is already {record.status.value}; there is "
                "nothing to pause"
            )
        self._paused.add(run_id)
        record.pause_requested = True
        await self.store.update_execution(record)
        logger.info("run %s: pause requested", run_id)

    @requires(Permission.FLOW_RUN)
    async def unpause(self, run_id: str) -> ExecutionResult:
        """Release a paused run and let it continue.

        Delivered as the ordinary ``resume:<run_id>`` event the pause parked on,
        so nothing about waking a held run is special-cased — it goes through
        the same suspension machinery, the same idempotency, and the same
        journal entry a ``ctx.wait_for_event`` would.
        """
        record = await self._require(run_id)
        self._paused.discard(run_id)
        if record.pause_requested:
            record.pause_requested = False
            await self.store.update_execution(record)
        await self.send_event(run_id, _resume_event(run_id))
        return await self._result_for(await self._require(run_id))

    def is_pause_requested(self, run_id: str) -> bool:
        """Whether this run has been asked to hold."""
        return run_id in self._paused

    def _is_being_driven(self, record: ExecutionRecord) -> bool:
        """Whether some process still holds a live lease on this run.

        A run with no lease, or one whose lease has expired, is nobody's: there
        is no body to raise ``WorkflowCancelled`` inside, so cancelling it means
        writing the terminal status directly. A leased run belongs to its
        driver, which is the only place compensations can run.
        """
        if record.run_id in self._driving:
            return True
        if record.lease_expires_at is None:
            return False
        return _as_utc(record.lease_expires_at) > self.clock.now()

    def is_cancellation_requested(self, run_id: str) -> bool:
        return run_id in self._cancelled

    # -- events -----------------------------------------------------------------------

    @requires(Permission.FLOW_RUN)
    async def send_event(
        self,
        run_id: str | None,
        name: str,
        payload: Any = None,
        *,
        dedupe_key: str | None = None,
        to_topic: bool = False,
        event_id: str | None = None,
        chain_depth: int = 0,
    ) -> EventDelivery:
        """Deliver an event. Resumes the target run if it is parked waiting for it.

        Pass *dedupe_key* — a broker message id, typically — when the sender is
        at-least-once. Kafka, Redis Streams, and SQS all are, and without a key
        a redelivered message resumes the run a second time. ``submit()`` has
        taken an ``idempotency_key`` since the beginning; this is the same
        protection for the event path.

        Pass ``to_topic=True`` to also append the event to :attr:`events` — the
        log an :class:`~loom.events.dispatcher.EventDispatcher` reads — under
        topic *name*, when one is configured. Only :meth:`Context.publish` does
        this. It must default to ``False`` for every other caller, and in
        particular for the dispatcher's own internal
        :meth:`~loom.events.dispatcher.EventDispatcher._wake_waiting`: that one
        re-sends an event the dispatcher just *read from* this same log to wake
        ``wait_for_event`` parkers, and appending it back under its own type
        would feed the dispatcher's next pass its own output — a self-sustaining
        loop with a fresh, uncapped ``chain_depth`` on every turn, immune to the
        cap this exists to enforce. *event_id* and *chain_depth* only matter
        when *to_topic* is set.

        Returns what happened rather than ``None`` because "already delivered"
        is a normal outcome an at-least-once consumer must be able to see: it
        is how the consumer knows to ack a redelivery rather than retry it.
        """
        if run_id is not None and not run_id:
            # A falsy target means "broadcast" below, and the stores persist it
            # as a row any run awaiting this name may take. `None` says that on
            # purpose -- ctx.publish waking every parker. An empty string is a
            # caller that meant to name a run and did not, so it is refused
            # here rather than becoming an approval left lying in the queue for
            # whichever run reaches that gate next.
            raise RegistryError(
                f"send_event needs a run id to deliver '{name}' to; got ''. "
                "Pass run_id=None to broadcast to every run awaiting it."
            )

        if dedupe_key is not None and not await self.store.claim_event_delivery(
            dedupe_key
        ):
            logger.info("event %s dropped: %s already delivered", name, dedupe_key)
            return EventDelivery(
                delivered=False, reason="duplicate", dedupe_key=dedupe_key
            )

        if to_topic and run_id is None and self.events is not None:
            from loom.events.models import EventRecord

            if payload is None:
                record_payload: dict[str, Any] = {}
            elif isinstance(payload, dict):
                record_payload = payload
            else:
                # `EventRecord.payload` is a mapping -- a subscription filter
                # and `dict(event.payload)` in the dispatcher both assume one.
                # A workflow publishing a scalar or a list still gets a topic
                # entry rather than a rejected call.
                record_payload = {"value": payload}

            await self.events.append(
                name,
                [
                    EventRecord(
                        event_id=event_id or f"loom:publish:{uuid.uuid4()}",
                        type=name,
                        payload=record_payload,
                        source="loom.publish",
                        chain_depth=chain_depth,
                    )
                ],
            )

        await self.store.enqueue_event(Event(name=name, payload=encode(payload), run_id=run_id))

        waiter = self._event_waiters.get((run_id or "", name))
        if waiter is not None:
            waiter.set()

        targets = [run_id] if run_id else await self.store.runs_awaiting_event(name)
        resumed: list[str] = []
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
                resumed.append(target)
                self._spawn(self._drive(target))
        return EventDelivery(delivered=True, run_ids=resumed, dedupe_key=dedupe_key or "")

    async def take_event(self, run_id: str, name: str) -> Event | None:
        event = await self.store.take_event(run_id, name)
        if event is not None and event.payload is not None:
            return event
        return event

    @requires(Permission.FLOW_RUN)
    async def approve(self, run_id: str, subject: str, *, approved: bool = True) -> None:
        """Resolve a pending human approval.

        Gated on ``FLOW_RUN`` rather than ``GRANT_APPROVE``: answering a human
        gate continues an execution, where ``GRANT_APPROVE`` is about widening
        what a run may *reach*. Held by OPERATOR and above, so the role whose
        job this is can do it and VIEWER — which could approve anything at all
        before this — cannot.
        """
        await self.send_event(run_id, f"approval:{subject}", {"approved": approved})

    # -- queries ----------------------------------------------------------------------

    @requires(Permission.RUN_VIEW)
    async def get(self, run_id: str) -> ExecutionRecord | None:
        return await self.store.get_execution(run_id)

    @requires(Permission.RUN_VIEW)
    async def result(self, run_id: str) -> ExecutionResult:
        return await self._result_for(await self._require(run_id))

    @requires(Permission.RUN_VIEW)
    async def list_runs(self, **filters: Any) -> list[ExecutionRecord]:
        return await self.store.list_executions(**filters)

    @requires(Permission.RUN_VIEW)
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
        dispatcher: Any | None = None,
    ) -> None:
        """Run the scheduler in the background until :meth:`shutdown`.

        Three things, on one loop: due timers (``ctx.sleep``), orphan recovery,
        and — when a *dispatcher* is passed — cron and interval triggers.

        Pass an *elector* to run many processes against one store: each tick
        first tries to take the lease for *group*, and only the holder scans.
        Without it every process would resume the same timers.

        The *dispatcher* argument exists because leaving cron outside this loop
        was a trap: a host that called ``start_scheduler(elector=…)``
        reasonably believed its scheduling was single-leader, and its timers
        were while its crons were not. Cron is still correct without an elector
        — the occurrence key sees to that — but it was doing the work N times
        and quietly saying nothing about it.
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
                        if dispatcher is not None:
                            # Before the timer scan: firing a trigger creates a
                            # run, and creating it first means it is picked up
                            # this tick rather than one interval later.
                            await dispatcher.tick()
                        await self.tick()
                        await self.reclaim_orphans()
                except Exception:
                    logger.exception("scheduler tick failed")
                await self.clock.sleep(to_seconds(interval))

        self._scheduler_task = asyncio.create_task(loop())

    async def __aenter__(self) -> Runtime:
        """Use a Runtime as a context manager, so shutdown cannot be forgotten.

            async with Runtime(store=MemoryStore()) as rt:
                await rt.run(my_flow, payload)

        Equivalent to a ``try/finally`` around :meth:`shutdown`, which is what
        every host needs and what a trailing ``await rt.shutdown()`` only does
        when nothing goes wrong — the case where it matters least. On the way
        out of an interrupted program that trailing call is the first thing
        skipped.
        """
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.shutdown()

    def supervise(self, service: Any) -> None:
        """Have :meth:`shutdown` stop this background service too.

        A service is anything with an async ``stop()`` that polls on its own
        task — :class:`TriggerDispatcher` and :class:`QueueConsumer` today. Both
        register themselves from ``start()``, so a host that shuts the Runtime
        down does not have to know which of them it happens to have wired up.

        Held weakly: a service the caller has dropped is one nobody can stop by
        name either, and keeping it alive here would leak every dispatcher a
        long-lived process ever started.
        """
        self._services.add(service)

    def unsupervise(self, service: Any) -> None:
        """Forget a service, because it has stopped itself."""
        self._services.discard(service)

    async def shutdown(self, *, drain: Duration = 5.0) -> None:
        """Stop background work and settle what is in flight.

        In order, because the order is the point: stop the *sources* of new runs
        (supervised dispatchers and consumers, then the scheduler), then let the
        drives already running finish, then cancel whatever is left.

        *drain* is how long in-flight drives get. A drive cancelled at the
        deadline is not lost — it leaves the run RUNNING with an expired lease,
        which the next :meth:`reclaim_orphans` on any node resumes from the
        journal. ``drain=0`` cancels immediately, which is what a test wants and
        what this did before it took an argument.
        """
        for service in list(self._services):
            stop = getattr(service, "stop", None)
            if stop is None:
                continue
            with contextlib.suppress(Exception):
                result = stop()
                if hasattr(result, "__await__"):
                    await result
        self._services.clear()

        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
            self._scheduler_task = None

        pending = [task for task in self._background if not task.done()]
        seconds = to_seconds(drain)
        if pending and seconds > 0:
            # Deliberately the real clock. A drain is a grace period against the
            # process actually exiting, and a ManualClock — which no one is
            # advancing during shutdown — would wait forever.
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=seconds)
            pending = [task for task in pending if not task.done()]

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
        if self.hooks:
            # Recorded on the *run*, never in the workflow version. Middleware
            # states what this deployment enforces, not what the workflow is —
            # folding it into `content_hash` would give one commit as many
            # versions as it has environments. But a denial still has to be
            # explicable months later, so what was in force gets written down
            # here. See docs/design/hooks-middleware.md, Q10.
            record.metadata["loom.middleware"] = self.hooks.names()
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
        if not dirty:
            return
        await self.store.save_journal(record.run_id, dirty)
        # The one point every journal flush passes through, and therefore the
        # only place that sees a run id beside the entries that name its blobs.
        # Costs nothing unless a payload was actually offloaded — see
        # loom.blobs.refcount.
        await record_refs(self.cache, record.run_id, dirty)

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
            await self._settle_lease(run_id)

    async def _drive_inner(self, run_id: str, *, deps: Any) -> ExecutionResult:
        record = await self._require(run_id)
        definition = self.resolve_workflow(record.workflow)
        refusal = self._version_refusal(record, definition)
        if refusal is not None:
            return await self._finish_failed(
                record,
                Journal(await self.store.load_journal(run_id)),
                refusal,
                definition,
                [],
            )

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
            # Two processes can both load this SUSPENDED record after the same
            # delivered event (e.g. a Kafka approval consumed twice) and both
            # reach here. Only one may win the SUSPENDED -> RUNNING transition
            # and start replaying the journal; the other must back off rather
            # than interleave writes into a journal it is not driving.
            previous_status = record.status
            resuming_from_suspend = previous_status == ExecutionStatus.SUSPENDED
            record.status = ExecutionStatus.RUNNING
            record.attempt += 1
            record.started_at = record.started_at or self.clock.now()
            record.wake_at = None
            record.awaiting_event = None
            record.lease_owner = self.node_id
            record.lease_expires_at = self.clock.now() + timedelta(seconds=self.lease_ttl)
            self._stamp_requests(record)
            try:
                await self.store.update_execution(
                    record,
                    expected_status=previous_status if resuming_from_suspend else None,
                )
            except ConcurrentUpdateError:
                logger.warning(
                    "run %s: lost the race to resume from SUSPENDED (another "
                    "process already advanced it) — backing off as a no-op",
                    run_id,
                )
                current = await self.store.get_execution(run_id)
                if current is None:
                    return await self._result_for(record)
                return await self._result_for(current)

            if record.pause_requested or run_id in self._paused:
                # Held before the body runs, so a paused run costs nothing
                # while it waits and resumes through the ordinary event path.
                self._paused.add(run_id)
                await self._park(
                    record,
                    Suspend(
                        f"run {run_id} is paused",
                        path="",
                        awaiting_event=_resume_event(run_id),
                    ),
                    journal,
                )
                return await self._result_for(record, journal)

            # Before the body re-enters, not after: a broker whose decision
            # depends on what the run has already done has to know that first,
            # and everything the journal can answer never reaches it.
            self.observe_run(run_id, journal)

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
                coro = self._invoke_body(definition, ctx, record, payload)
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
                parked = Suspend(
                    str(expired),
                    path="",
                    awaiting_event=f"credential:{expired.name}" if expired.name else None,
                )
                await self.persist_journal(record, journal)
                should_continue = await self._park(record, parked, journal)
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
            except asyncio.CancelledError:
                # Not a workflow outcome — the *process* is going away: Ctrl+C, a
                # SIGTERM handler, or shutdown() cancelling this drive. The run
                # has neither failed nor been cancelled by anyone with an opinion
                # about it, so nothing is written to its status; it stays RUNNING
                # and _settle_lease hands it to the next reclaim_orphans.
                #
                # CancelledError is a BaseException, so it reached here only
                # because it is named. Falling into the `except Exception` arm
                # below would be worse than missing it: the run would be recorded
                # FAILED on a keystroke, and the compensation stack would unwind
                # work that is about to be resumed and completed.
                span.set_status("abandoned")
                span.end()
                # Best-effort, and only load-bearing above the default
                # flush_every=1 — under it every durable op is already on disk
                # and the sole casualty is the step that was in flight, which
                # produced nothing to keep.
                with contextlib.suppress(Exception):
                    await self.persist_journal(record, journal)
                raise
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

    def capabilities(self) -> dict[str, bool]:
        """Which optional ports this Runtime actually has wired.

        There was no way to ask. Fifteen of ``Runtime.__init__``'s fifty-one
        keyword parameters are optional ports, and nodes reach them through
        ``getattr(runtime, name, None)`` driven by a string map — so mypy sees
        none of them, a rename passes every gate and fails at run time inside
        somebody's workflow, and an operator could not find out what their
        deployment could do without reading the constructor.

        Read by ``NodeRegistry.check_requirements`` through the same names, so
        this and the requirement check cannot disagree about what "configured"
        means.
        """
        return {
            name: getattr(self, name, None) is not None
            for name in CAPABILITY_PORTS
        }

    def missing_capabilities(self, *names: str) -> list[str]:
        """Which of *names* this Runtime does not have, in the order asked."""
        wired = self.capabilities()
        return [name for name in names if not wired.get(name, False)]

    def _version_refusal(
        self,
        record: ExecutionRecord,
        definition: WorkflowDefinition[Any, Any, Any],
    ) -> BaseException | None:
        """Whether this run may be driven by the code this process holds.

        ``None`` means proceed. Anything else is refused before the body runs,
        which is the only useful moment — after the first durable call there is
        already a journal entry written by code the run did not start with.

        The comparison costs nothing and needs no version store: a run records
        its definition's ``code_hash`` when it is created, and the current
        definition carries its own. What the store is needed for is
        :attr:`VersionPolicy.PINNED`, which has to *fetch* the old source.
        """
        recorded = record.code_hash
        if not recorded or recorded == definition.code_hash:
            return None

        record.metadata["loom.code_changed"] = {
            "started_with": recorded,
            "now": definition.code_hash,
        }

        if self.version_policy is VersionPolicy.LATEST:
            logger.warning(
                "run %s started under %s of workflow '%s' and this process has "
                "%s. Resuming against the current code (version_policy=latest); "
                "a divergent journal is still refused by compatibility and "
                "verification.",
                record.run_id,
                recorded[:12],
                record.workflow,
                definition.code_hash[:12],
            )
            return None

        from loom.runtime.versions import CodeChanged

        if self.version_policy is VersionPolicy.PINNED:
            # Deliberately not a silent fall back to LATEST. Asking to be
            # pinned and being run against something else is the one outcome
            # this dial exists to make impossible.
            return CodeChanged(
                f"run {record.run_id} started under code_hash {recorded[:12]} "
                f"of '{record.workflow}' and this process has "
                f"{definition.code_hash[:12]}. version_policy=pinned needs the "
                "original source from a VersionStore; publish workflows with "
                "rt.publish(flow, source=...) so a run can be resumed against "
                "the code that started it."
            )

        return CodeChanged(
            f"run {record.run_id} started under code_hash {recorded[:12]} of "
            f"'{record.workflow}' and this process has "
            f"{definition.code_hash[:12]}. version_policy=refuse will not "
            "resume a run whose body changed underneath it. Deploy the "
            "original code to drain this run, or set "
            "version_policy=VersionPolicy.LATEST to accept the change."
        )

    def observe_run(self, run_id: str, journal: Journal) -> None:
        """Let a stateful broker re-derive this run's state from the journal.

        Called at re-entry and again whenever the journal gains an answer that
        can change a decision — an event arriving mid-body is the case that
        matters, because an approval is journaled by ``wait_for_event`` and
        never dispatched. At re-entry that entry is still ``SUSPENDED``: it
        becomes the human's "yes" only while the body is running, which is after
        the last chance a re-entry hook would have had to see it.

        Re-deriving rather than notifying keeps one source of truth. A broker
        told "an approval happened" would be maintaining a second history beside
        the journal, and the two would disagree the first time a run was
        replayed.
        """
        if isinstance(self.broker, RunObserver):
            self.broker.observe_run(run_id, journal)

    async def _invoke_body(
        self,
        definition: WorkflowDefinition[Any, Any, Any],
        ctx: Context[Any],
        record: ExecutionRecord,
        payload: Any,
    ) -> Any:
        """Invoke the workflow body, with body hooks around it.

        The hooks wrap here rather than at each of ``_drive_inner``'s seven
        exits, so "the body ended" is one place and every way out of it —
        parking, cancellation, rotation, failure, abandonment — is covered by
        construction rather than by remembering to add a call.
        """
        if not self.hooks.has_body:
            return await self._run_body(definition, ctx, record, payload)

        from loom.runtime.hooks import BodyContext

        hook_ctx = BodyContext(
            run_id=record.run_id,
            workflow=record.workflow,
            attempt=record.attempt,
            # The journal having entries is the honest signal, and it is true
            # for a resume, a retry and a replay alike — all the cases where
            # "this workflow started" would be a lie.
            re_entry=len(ctx._journal) > 0,
            input=payload,
        )
        await self.hooks.dispatch_body("start", hook_ctx)
        try:
            output = await self._run_body(definition, ctx, record, payload)
        except BaseException as exc:
            hook_ctx.status = _BODY_EXITS.get(type(exc).__name__, "failed")
            hook_ctx.error = exc
            await self.hooks.dispatch_body("end", hook_ctx)
            raise
        hook_ctx.status = "completed"
        hook_ctx.output = output
        await self.hooks.dispatch_body("end", hook_ctx)
        return output

    async def _run_body(
        self,
        definition: WorkflowDefinition[Any, Any, Any],
        ctx: Context[Any],
        record: ExecutionRecord,
        payload: Any,
    ) -> Any:
        """Invoke the workflow body through the sandbox seam.

        The default path is the one every existing Runtime already took —
        ``InlineSandbox`` awaits ``definition.invoke`` and hands the value
        back — so having the seam costs an attribute lookup and a dataclass.
        Exceptions are *not* caught here: parking, cancellation, and failure are
        three different things to the caller, and this method is on the path of
        every run in every deployment.
        """
        if isinstance(self.sandbox, InlineSandbox):
            # Skip recovering source entirely. It is only meaningful to an
            # adapter that runs the body elsewhere, and reading a module off
            # disk on every re-entry would be a cost paid by everyone for a
            # feature almost nobody has switched on.
            body = SandboxBody(invoke=lambda: definition.invoke(ctx, payload))
        else:
            body = SandboxBody(
                invoke=lambda: definition.invoke(ctx, payload),
                source=await self._sandbox_source(definition, record),
                entrypoint=definition.name,
                # A string sentinel per host-injected step: generated source
                # whose import of the step was stripped for sandboxing still
                # references it by bare name, and `Ctx._named(sentinel)`
                # recovers exactly that name from the string itself.
                namespace={name: name for name in self._sandbox_steps_extra},
            )

        outcome = await self.sandbox.run(
            body=body,
            run_id=record.run_id,
            input=payload,
            # The parent's live Context, so a proxied call journals at the same
            # path and reaches the broker by the same route as an inline one.
            channel=RuntimeChannel(ctx=ctx, steps=self._sandbox_steps(definition)),
            policy=self.sandbox_policy,
        )
        if outcome.ok:
            return outcome.value
        if outcome.violation:
            raise SandboxViolation(
                f"{definition.name} was stopped by its sandbox "
                f"({outcome.violation}): {outcome.error}"
            )
        raise WorkflowError(outcome.error or f"{definition.name} failed in its sandbox")

    async def _sandbox_source(
        self, definition: WorkflowDefinition[Any, Any, Any], record: ExecutionRecord
    ) -> str:
        """The body's source text: what was published, else what is on disk.

        Published first, deliberately. That is the code a host reviewed and
        pinned, and for a run resumed on another node it may be the only copy
        that exists there. The module on disk is the fallback for the ordinary
        case of a workflow that was never published.
        """
        if record.code_hash:
            with contextlib.suppress(Exception):
                version = await self.versions.resolve(record.workflow, record.code_hash)
                if version is not None:
                    published = await self.versions.source_of(version)
                    if published:
                        return str(published)

        module = inspect.getmodule(definition.fn)
        if module is None:
            return ""
        try:
            return inspect.getsource(module)
        except (OSError, TypeError):
            # A body defined in a REPL or exec'd string has no readable source.
            # Returning "" lets the sandbox refuse by name rather than run
            # something else.
            return ""

    def _sandbox_steps(
        self,
        definition: WorkflowDefinition[Any, Any, Any],
    ) -> dict[str, Any]:
        """The steps a sandboxed body is allowed to reach.

        Taken from the workflow function's own globals rather than from a
        global registry: the map is also the allowlist — a body can only
        invoke what is in it — and a process-wide registry would hand every
        sandboxed workflow every step any import had ever defined.

        ``fn.__globals__`` rather than ``inspect.getmodule(fn)``: a workflow
        compiled from a string with ``exec()`` (the coding agent's output, or
        a definition rebuilt from a published source) has no module — its
        function's ``__globals__`` *is* the exec'd namespace, and is where its
        steps live either way. A real module's functions have their module's
        ``__dict__`` as ``__globals__``, so this is a strict generalisation
        of the previous lookup, not a different one.

        ``Runtime(sandbox_steps=...)`` is merged in on top, for a step the
        workflow reaches without it being a name in its own globals — a host
        bridging a dynamic tool registry through one shared step, which the
        code calls by import but which the *sandbox* needs handed to it
        explicitly because there is no fixed module to scan for it either.
        Unfiltered, deliberately: an injected value need not be a
        ``StepDefinition`` — ``Context.step`` accepts a bare async callable
        too, wrapping it the same way it would wrap one found by scanning."""
        steps = {
            value.name: value
            for value in definition.fn.__globals__.values()
            if isinstance(value, StepDefinition)
        }
        steps.update(self._sandbox_steps_extra)
        return steps

    def _stamp_requests(self, record: ExecutionRecord) -> None:
        """Copy pause/cancel intent onto the record before the drive writes it.

        Without this the two are lost. ``pause()`` and ``cancel()`` load their
        own copy of the record, set a flag and save it — while the drive holds a
        copy from before, and overwrites the flag on its next update. The intent
        lives in this process's sets from the moment it is observed (either
        directly or via the lease heartbeat), so stamping it here is what makes
        the persisted record agree with what the process is actually doing.
        """
        record.pause_requested = record.run_id in self._paused
        record.cancel_requested = record.run_id in self._cancelled

    async def _park(self, record: ExecutionRecord, suspension: Suspend, journal: Journal) -> bool:
        """Persist a suspension. Returns True if we should immediately re-enter the body."""
        record.status = ExecutionStatus.SUSPENDED
        record.wake_at = suspension.wake_at
        record.awaiting_event = suspension.awaiting_event
        record.usage = journal.total_usage()
        self._stamp_requests(record)
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
        await self._release_admission(record)
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
        await self._release_admission(record)
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
        await self._release_admission(record)
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
        await self._release_admission(record)
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
                    self.run(handler, envelope, trigger=TriggerKind.ERROR_HANDLER)
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
            if record.pause_requested:
                self._paused.add(run_id)
            if record.cancel_requested:
                # Somebody cancelled this run, possibly from another process.
                # Recording it locally is what makes the next durable call
                # raise `WorkflowCancelled` inside *this* body — which is the
                # only place the compensation stack can unwind.
                self._cancelled.add(run_id)
            if record.lease_owner != self.node_id:
                # Someone else took over; stop touching their run.
                return
            record.lease_expires_at = self.clock.now() + timedelta(seconds=self.lease_ttl)
            await self.store.update_execution(record)

    async def _settle_lease(self, run_id: str) -> None:
        """Close out our claim on a run the drive has stopped working on.

        Two outcomes, and which one applies is read from the record rather than
        passed in, because every normal exit from ``_drive_inner`` has already
        moved the record off RUNNING — terminal via ``_finish_*``, SUSPENDED via
        ``_park``. So a record still RUNNING when the drive ends means the drive
        did not end normally: cancelled by Ctrl+C, by a SIGTERM handler, or by
        :meth:`shutdown`.

        *Settled* — drop the claim, so the run is not mistaken for an orphan we
        abandoned. *Abandoned* — record ``lease_owner`` as the breadcrumb naming
        who dropped it and expire the lease **now**, so the next
        :meth:`reclaim_orphans` on any node picks the run up immediately.

        Clearing the lease in the abandoned case is what made Ctrl+C worse than
        ``kill -9``: ``reclaim_orphans`` matches on ``lease_expires_at``, so a
        nulled one is unmatchable and the run stays RUNNING forever, with no
        timer covering it and nothing to resume it. A killed process leaves the
        lease intact and recovers; an interrupted one has to as well. Expiring
        immediately rather than leaving the heartbeat's future timestamp is the
        one improvement over the crash path: we *know* we are not coming back,
        so there is nothing to wait out.
        """
        record = await self.store.get_execution(run_id)
        if record is None:
            return
        # Someone else has taken it over, so it is no longer ours to settle.
        if record.lease_owner is not None and record.lease_owner != self.node_id:
            return

        if record.status not in _UNFINISHED:
            if record.lease_owner is None:
                return
            record.lease_owner = None
            record.lease_expires_at = None
        else:
            # PENDING as well as RUNNING, and the owner is *claimed* here rather
            # than assumed: a drive cancelled before its first store write
            # leaves a record nobody ever leased, which — like the nulled lease
            # above — no scan can match. Narrow, since only a store round-trip
            # sits between creating the record and taking the lease, but it is
            # the same defect and it deserves the same answer.
            record.lease_owner = self.node_id
            record.lease_expires_at = self.clock.now()
        await self.store.update_execution(record)

    async def reclaim_orphans(
        self,
        now: datetime | None = None,
        *,
        limit: int = 100,
        scan_limit: int = 10_000,
    ) -> list[str]:
        """Resume unfinished runs abandoned by a node that died or was stopped.

        A crashed worker leaves its run marked RUNNING forever — no timer covers
        it, because it is not waiting for one. This finds records whose lease has
        expired and re-drives them; the journal makes that safe, since everything
        already completed is served from it rather than repeated.

        ``PENDING`` is scanned as well as ``RUNNING``, for the run whose drive
        was cancelled before it could take the lease. What keeps that safe is
        that the expired lease — not the status — is the signal: a record freshly
        created by ``submit()`` and still queued has no lease at all, so it is
        never matched, however long it sits there. Only a drive that reached its
        ``finally`` leaves one behind (see :meth:`_settle_lease`), which is
        exactly the set of runs somebody has stopped working on.

        Call from a scheduler loop. Returns the run ids picked up.

        ``limit`` bounds how many runs are *reclaimed*, not how many are looked
        at. That distinction is the whole correctness of this method. The lease
        lives inside the record's JSON payload, so it cannot be a ``WHERE``
        clause and the filtering happens here — and it used to happen after a
        single ``list_executions(limit=limit)``, which orders newest-first
        because ``run_id`` is a ULID. An orphan is by definition a run that
        stopped advancing, so orphans hold the *oldest* ids and were the first
        to fall out of that window: with more than ``limit`` healthy runs in
        flight, the exact set this exists to rescue became the set it could not
        see, and it returned ``[]`` while reporting success.

        So it pages to exhaustion instead, bounded by ``scan_limit`` for the
        deployment where "all unfinished runs" is very large. Hitting that
        ceiling is logged rather than passed over: a reclaim that quietly
        examined a prefix is the bug being fixed, one page bigger.
        """
        moment = now or self.clock.now()
        stale: list[ExecutionRecord] = []
        scanned = 0
        truncated = False

        indexed = getattr(self.store, "due_leases", None)
        if indexed is not None:
            # The predicate pushed into the store, where migration 1 gave it a
            # column and an index. Whole answer, one round trip.
            for record in await indexed(moment, _UNFINISHED, limit=limit):
                if record.run_id not in self._driving:
                    stale.append(record)
            return await self._reclaim(stale[:limit])

        # A host's own ExecutionStore need not implement it; paging to
        # exhaustion is correct, just linear.
        for status in _UNFINISHED:
            offset = 0
            while True:
                if scanned >= scan_limit:
                    # Exactly one row, only ever on the ceiling path: it is the
                    # difference between "the data ended here" and "we stopped
                    # here", and reporting the second as the first is the defect
                    # this method is being fixed for.
                    truncated = bool(
                        await self.store.list_executions(
                            status=status, limit=1, offset=offset
                        )
                    )
                    break
                page = await self.store.list_executions(
                    status=status,
                    limit=min(_RECLAIM_PAGE, scan_limit - scanned),
                    offset=offset,
                )
                if not page:
                    break
                scanned += len(page)
                offset += len(page)
                for record in page:
                    if (
                        record.run_id not in self._driving
                        and record.lease_expires_at is not None
                        and _as_utc(record.lease_expires_at) <= moment
                    ):
                        stale.append(record)
                if len(stale) >= limit:
                    break
            if truncated or len(stale) >= limit:
                break

        if truncated:
            logger.warning(
                "reclaim_orphans stopped after scanning %d unfinished runs "
                "(scan_limit=%d); some orphaned runs may not have been examined. "
                "Raise scan_limit, or shorten retention on unfinished runs.",
                scanned,
                scan_limit,
            )

        return await self._reclaim(stale[:limit])

    async def _reclaim(self, stale: list[ExecutionRecord]) -> list[str]:
        """Re-drive each abandoned run. Shared by both scan paths."""
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

    async def _release_admission(self, record: ExecutionRecord) -> None:
        """Free the in-flight slot a concurrency or singleton policy reserved,
        and drop any per-run state a broker was keeping.

        Called only from terminal transitions — a suspended run still occupies
        its slot, which is what makes ``singleton`` mean "one live run" rather
        than "one running instruction". A broker's per-run state follows the
        same rule for the same reason, and releasing it here rather than in
        four separate ``_finish_*`` methods is what keeps the two from drifting
        apart; a map keyed by run id and never cleared is a leak the size of the
        process's lifetime.
        """
        if isinstance(self.broker, RunObserver):
            self.broker.forget_run(record.run_id)
        if self.admission is None:
            return
        definition = self._workflows.get(record.workflow)
        if definition is None or definition.flow_control is None:
            return
        await self.admission.record_end(
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
            await self.admission.record_start(definition.name, partition)
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
        spawned: asyncio.Task[Any] = task
        return spawned


def _backend_from_env() -> Any | None:
    """Build an agent backend from whichever provider key is present.

    ``None`` when no key is set, or when the pieces are not installed — a
    Runtime without an agent backend is a valid Runtime, and most workflows
    never call one.
    """
    from loom.agents import providers

    provider = providers.from_env()
    if provider is None:
        return None
    try:
        from loom.agents.backend import BuiltInBackend
    except Exception:  # pragma: no cover - depends on env
        return None
    return BuiltInBackend(model=provider)


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

