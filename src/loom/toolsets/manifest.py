"""Toolset manifest — the declarative description of an integration.

A ``ToolsetManifest`` captures every operation an external service exposes,
grouped by resource (e.g. ``leads``, ``contacts``).  The manifest is the
source of truth for the three-tier disclosure catalog, code generation,
certification, and grant derivation.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from loom.toolsets.kinds import ToolsetKind


class EffectClass(StrEnum):
    """Classifies the side-effect of an operation."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class OperationSpec(BaseModel):
    """A single operation within a toolset group."""

    id: str
    """Dot-separated identifier, e.g. ``leads.upsert``."""
    function: str = ""
    """Name of the ``@step`` function implementing this operation.

    An operation id names a capability; it is not something anyone can write in
    Python. Without this the generated documentation shows only ``leads.upsert``
    and a model asked to write code invents an import to match it. Set together
    with :attr:`ToolsetManifest.tools_module` to make the operation callable
    from generated code rather than merely describable."""
    summary: str
    """One-line description (~40 tokens)."""
    description: str = ""
    """Full documentation."""
    effect: EffectClass = EffectClass.WRITE
    """What this operation does to the world. **Declare it on every operation,
    including the reads** — the default is a backstop, not a classification.

    It defaults to ``WRITE`` because the alternative fails open. ``READ`` is the
    one class exempt from every write and destructive control, so defaulting to
    it means an operation nobody classified is *granted* rather than flagged: a
    forgotten ``delete`` is reachable by an agent resolved with
    ``resolve_tools(effects={EffectClass.READ})``. Defaulting to ``WRITE`` makes
    the failure mode a refused call instead, which is recoverable by reading the
    error. ``EffectCall.effect`` in ``runtime/effects.py`` has always defaulted
    this way; this makes the manifest agree with the broker.

    All 320 operations LOOM ships declare this explicitly, so the default is
    reached only by a toolset that has not been classified yet — which is
    exactly the case that should not be trusted with a read-only grant.
    """
    input_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the input payload (if no Pydantic model)."""
    output_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the output payload."""
    reversible: bool = False
    """Can this operation's effect be undone, restoring the prior state?

    **Not** "is there an opposite operation". Deleting an issue you created
    does not undo the create — the key is consumed, the comments are gone — so
    ``issues.create`` is not reversible by ``issues.delete``. Only a genuine
    restore counts: trash/untrash, share/unshare.

    This is the axis ``EffectClass`` cannot express, and the one that matters
    most when a model is choosing. Ranked by damage, ``gmail_trash_message`` is
    DESTRUCTIVE and ``gmail_send_message`` is WRITE — but trashing is
    recoverable for thirty days and nothing unsends an email. A policy that
    blocks DESTRUCTIVE and permits WRITE therefore stops the recoverable
    operation and allows the irreversible one.

    Declared, never derived. Whether an inverse genuinely restores the prior
    state is a judgement about the service, and :func:`~loom.toolsets.certify`
    checks only that the id resolves (CERT-14)."""

    undone_by: str = ""
    """The operation id that reverses this one, when one exists.

    Richer than the boolean and checkable — and eventually actionable: this is
    what ``ctx.compensate()`` would register to unwind a failed saga, which is
    machinery LOOM already has, wired to a declaration it already needs."""

    access_control: bool = False
    """Does this change *who can reach data*, rather than the data itself?

    ``share`` / ``unshare`` / ``invite`` / ``remove_permission``. AWS promotes
    the same idea to a top-level access level (``Permissions management``)
    rather than a flavour of Write, and for an agent it is the highest-
    consequence category available: **sharing a folder exfiltrates without
    writing anything to it**, and reads as an ordinary additive write.

    Declared, never derived. A scope-based derivation was measured against all
    320 shipped operations and matched **zero** — Google covers permissions
    with the broad scope, so ``drive_share_file`` declares exactly what an
    ordinary write declares, and the Microsoft toolsets declare no scopes at
    all. A name-based one is the F3 mistake in a second place."""

    effect_by: dict[str, dict[str, EffectClass]] = Field(default_factory=dict)
    """Argument-dependent effect: ``{"method": {"GET": READ, "DELETE": DESTRUCTIVE}}``.

    :attr:`effect` is a property of the operation; for a few it is a property
    of the *call*. ``io.http_request`` is one node with one class, and
    ``method="GET"`` is a read while ``method="DELETE"`` destroys — and it is
    precisely the node a generated workflow reaches for when no toolset covers
    the API.

    Declarative rather than a callable, deliberately: grant validation and the
    catalog read manifest metadata without importing a toolset, and a callable
    would need the module.

    A matched rule wins in **either** direction — ``GET`` lowers the class and
    ``DELETE`` raises it, and both are the author's own declaration about their
    own operation. :attr:`effect` is the fallback, used whenever the argument
    was not passed or its value is not in the table, so an unrecognised method
    keeps the cautious class rather than falling to a read.
    """

    open_world: bool = True
    """Does this reach outside the deployment's trust boundary?

    ``True`` for anything that calls a remote service — which is every
    operation in a toolset, so it is only worth setting to ``False`` on a
    manifest wrapping computation the deployment already owns.

    What reads it is the read-to-write taint rule. That rule keys on *reading
    the world*, and it used to approximate that as ``EffectClass.READ``, which
    is not the same thing: filtering a list the run was handed is a READ and
    reads nothing. MCP names the same axis ``openWorldHint`` and gives the same
    example — a web search is open, a memory tool is not.
    """

    scopes: list[str] = Field(default_factory=list)
    """Required OAuth / API scopes."""
    pagination: bool = False
    """Whether this operation returns a page of a larger result set.

    Set it on every read that can return more rows than one request carries.
    The toolset client follows the pages to fill the caller's limit and hands
    back a :class:`~loom.toolsets.pagination.Results`, which knows
    whether it saw everything — but a caller only asks that question if it
    knows there was a question, so this is what puts "may be capped" in front
    of whoever is writing the call.

    A read that returns one object, or a naturally bounded list, leaves it
    ``False``. Declaring pagination that does not happen is as misleading as
    omitting it."""
    rate_limit_group: str = ""
    """Shared rate-limit key across ops that share a quota."""
    idempotent: bool = False
    """Safe to retry without side-effects?"""
    resolves: str = ""
    """The kind of entity this turns a human's words into a stable identifier for.

    ``"user"`` on an operation that takes a name and returns an account id.
    Filtering on a person, a project, or a board by the name someone typed is
    the single most common way a query returns zero rows and no error — the API
    matches on identifiers, the human said a display name, and nothing joins
    them. Marking the operation lets a caller be told to resolve first, without
    knowing anything about this particular service."""



class AuthField(BaseModel):
    """One environment variable a toolset's client reads.

    The fallback path, and for 21 of the 27 toolsets LOOM ships it is the only
    path — so it is a declaration rather than documentation: ``loom doctor``
    and ``ConnectionInspector`` answer "is this configured?" out of it, and
    neither can read a docstring.
    """

    name: str
    """The variable, e.g. ``JIRA_API_TOKEN``."""
    label: str = ""
    """What to call it when asking a person for it. The variable name is what
    the machine needs; ``Atlassian API token`` is what someone recognises."""
    mode: str = ""
    """Which credential *mode* this field belongs to, when a toolset has more
    than one.

    Several services accept alternatives: Google takes a ready-made access
    token, **or** a client id/secret/refresh-token trio, **or** a service
    account file; Microsoft takes `MS_*`, **or** the `AZURE_*` names the Azure
    SDKs already put in an environment, **or** a ready-made Graph token.
    `required` alone cannot say that — marking all of them required reports a
    working deployment as missing five variables, and marking them optional
    reports an empty one as configured.

    Fields sharing a non-empty `mode` are **one** way of authenticating and are
    all needed for it; a toolset is satisfied by any one complete mode.
    Ungrouped `required` fields are needed by every mode.

    `GoogleCredentials.mode` and `MicrosoftCredentials.mode` are the same idea
    in the clients, and these values mirror theirs — which is what
    `tests/test_manifest_auth.py` holds them to.
    """
    secret: bool = True
    """Whether the value is a credential. Drives whether it is prompted for
    without echo, kept out of a rendered summary, and redacted from a trace.
    Defaults to **true**: a field wrongly treated as secret costs a masked
    prompt, and one wrongly treated as public is a credential on screen."""
    required: bool = True
    example: str = ""
    arg: str = ""
    """The client constructor keyword this value feeds, e.g. ``base_url``.

    What turns construction into one generic function instead of twenty-seven
    factories kept in step by hand — and a *declaration* for the reason
    :attr:`OperationSpec.effect` and :attr:`ToolsetManifest.tools_module` are:
    ``JIRA_URL -> base_url`` and ``JIRA_API_TOKEN -> api_token`` cannot be
    derived from the names, and a rule that lowercases and strips the prefix is
    right for some toolsets and silently wrong for the rest.

    Empty where :attr:`AuthSpec.credentials` carries the mapping instead — the
    Google and Microsoft clients take a credentials object whose own
    ``from_env`` already names each variable, and repeating that here would be
    a second copy to drift.
    """


class AuthSpec(BaseModel):
    """How a toolset is authenticated. **Declared, never inferred.**

    The position :attr:`OperationSpec.effect` already takes, for the same
    reason. ``jira`` is served by the ``atlassian`` OAuth provider, ``gmail``
    by ``google_gmail``, and ``teams``/``onedrive``/``sharepoint``/``onenote``/
    ``outlook_mail``/``outlook_calendar`` all by ``microsoft`` — thirteen of
    the twenty-odd toolsets that have a provider do not share its name, so a
    rule derived from the id is right for a minority and silently wrong for
    the rest. ``loom connect jira`` refused with *"'jira' is not a known
    provider"* for exactly that reason.

    Replaces a free-form ``dict[str, Any]`` whose shape varied per toolset:
    some declared ``credential``, most declared only ``fields``, one a
    ``header``, one a ``grant``. Nothing could answer "what does this toolset
    need, and does this machine have it?" A legacy dict is still accepted and
    promoted — see :meth:`_accept_legacy_dict` — so a third-party manifest
    written against the old shape keeps working.
    """

    kind: Literal["none", "api_key", "basic", "bearer", "oauth2"] = "none"
    """The wire scheme. ``none`` means the API needs no credential at all."""

    credential: str = ""
    """The :class:`~loom.connectors.credentials.CredentialStore` key this
    toolset's client actually reads, or ``""`` when it reads none.

    The single fact that was nowhere: ``jira/client.py`` defaults
    ``credential_name="jira"`` and nothing outside that file could learn it.

    **Empty is a statement, not an omission.** 21 of the 27 shipped toolsets
    have no store path at all — they read environment variables and nothing
    else — and declaring a name they never look up would make ``loom connect``
    report success for a credential the toolset cannot see, which is worse
    than saying so. ``tests/test_manifest_auth.py`` checks this against the
    client source in both directions."""

    provider: str = ""
    """:attr:`~loom.connectors.oauth_providers.OAuthProviderConfig.id`, when
    ``kind`` is ``oauth2`` and a browser flow can obtain the credential.

    Empty on an ``oauth2`` toolset means the token is obtained some other way
    — Zoom's server-to-server grant, a Google service account, a Salesforce
    org-specific instance — rather than that nobody filled it in."""

    scopes: tuple[str, ...] = ()
    """What *this toolset* needs, which is narrower than the provider's
    defaults. A Jira-only workflow should not be made to grant Confluence."""

    fields: tuple[AuthField, ...] = ()
    """The environment variables the client reads when nothing is connected."""

    client: str = ""
    """Dotted path to the client class, ``module:Class``.

    Mirrors :attr:`ToolsetManifest.tools_module`, and for the same reason: a
    manifest that cannot say how to reach its own implementation leaves every
    caller to guess at one. With it, ``client_for("jira")`` is a single generic
    function; without it, the only way to build a client is the module-level
    singleton each toolset keeps, which reads the environment once per process
    and caches the result for the life of it."""

    credentials: str = ""
    """Dotted path to a credentials class, when the client takes one.

    Two shapes exist and this is what tells them apart. Most clients take their
    values directly — ``JiraClient(base_url=…, email=…, api_token=…)`` — and map
    them through :attr:`AuthField.arg`. The Google, Microsoft and Zoom clients
    take a single ``auth`` object instead, and that object's ``from_env`` already
    maps every variable to an attribute, including the multi-mode logic that
    ``AuthField.mode`` mirrors. Naming the class here reuses that mapping rather
    than restating it field by field."""

    setup_url: str = ""
    """Where a person creates the app or the API key. The step nobody can do
    for them, and the one a "not connected" message has to name."""
    docs_url: str = ""

    @model_validator(mode="after")
    def _a_provider_needs_somewhere_to_put_the_token(self) -> AuthSpec:
        """A browser flow that stores a token nothing reads is worse than none.

        `provider` says a connect flow can obtain this credential; `credential`
        says where the client looks for one. Declaring the first without the
        second produces the failure this whole area exists to remove: the flow
        opens a browser, the person authorises, a token is written, "Connected"
        is printed — and every call still 401s against an environment variable
        that was never set.

        It is a real shape: `github` and `hubspot` both have an OAuth provider
        in the registry and clients that read no `CredentialStore` at all.
        They declare neither, and giving them a store path is a change to the
        client rather than to a manifest.
        """
        if self.provider and not self.credential:
            raise ValueError(
                f"auth declares provider={self.provider!r} but no credential: a "
                "connect flow would store a token this toolset's client never "
                "reads. Declare the CredentialStore key the client uses, or "
                "drop the provider."
            )
        return self

    def satisfied_by(self, environ: Mapping[str, str]) -> tuple[bool, tuple[str, ...]]:
        """Whether *environ* configures this toolset, and what is short if not.

        The missing list is for the **nearest** mode, because a person reading
        it wants the shortest path to working rather than a union of every
        alternative: a deployment holding a valid refresh token should not be
        told it needs an access token and a service account file as well.

        Nearest is fewest-missing, then **most already present** — and the
        tie-break is the half that matters. Two thirds of the way through
        Google's client-id/secret/refresh trio, every mode is one variable
        short, so counting alone picks whichever was declared first and answers
        "set GOOGLE_ACCESS_TOKEN" to somebody plainly in the middle of the
        other one.
        """
        _, missing = self.nearest_mode(environ)
        return not missing, missing

    def nearest_mode(
        self, environ: Mapping[str, str]
    ) -> tuple[str, tuple[str, ...]]:
        """The mode *environ* comes closest to satisfying, and what it lacks.

        Split out of :meth:`satisfied_by` because resolution needs the half
        that method threw away. "Is this configured?" and "which of the three
        ways is it configured?" are answered by the same walk, and computing
        the second separately would be a second copy of the tie-break — the
        subtle half, where two thirds of the way through Google's trio every
        mode is one variable short.

        The mode name is ``""`` when the toolset declares no alternatives,
        which is most of them.
        """
        def present(name: str) -> bool:
            return bool(environ.get(name))

        base = tuple(
            f.name for f in self.fields if f.required and not f.mode and not present(f.name)
        )
        modes: dict[str, list[AuthField]] = {}
        for field in self.fields:
            if field.mode:
                modes.setdefault(field.mode, []).append(field)
        if not modes:
            return "", base

        def shortfall(item: tuple[str, list[AuthField]]) -> tuple[int, int, str]:
            name, fields = item
            absent = tuple(f.name for f in fields if not present(f.name))
            return len(absent), -(len(fields) - len(absent)), name

        chosen = min(modes.items(), key=shortfall)[0]
        nearest = tuple(f.name for f in modes[chosen] if not present(f.name))
        return chosen, base + nearest

    @property
    def secret_fields(self) -> tuple[AuthField, ...]:
        return tuple(f for f in self.fields if f.secret)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_dict(cls, value: Any) -> Any:
        """Promote the old free-form shape rather than rejecting it.

        ``{"type": "basic", "fields": ["JIRA_URL", ...]}`` — plus whichever of
        ``credential``/``header``/``grant``/``token_url`` that toolset happened
        to add. Everything unrecognised is dropped rather than carried: it was
        read by nothing, and keeping it would preserve the ambiguity this
        class exists to remove.
        """
        if not isinstance(value, dict):
            return value
        if "type" not in value:
            # Not legacy — these are this model's own keyword arguments, and
            # `kind` is one of them. Detecting on `kind` instead of `type` is
            # the same word meaning two things: it made every *typed*
            # construction take the promotion path, which stringified each
            # `AuthField` into a `name` and defaulted `secret` back to true.
            # A bare string is still accepted where a field is expected,
            # because writing `fields=("STRIPE_API_KEY",)` is a reasonable
            # thing to try and refusing it teaches nothing.
            fields = value.get("fields")
            if fields and any(isinstance(f, str) for f in fields):
                return {
                    **value,
                    "fields": tuple(
                        {"name": f} if isinstance(f, str) else f for f in fields
                    ),
                }
            return value
        legacy = dict(value)
        kind = str(legacy.pop("type", "none") or "none")
        if kind == "token":
            # ClickUp and GitLab: a personal token or an OAuth one, sent as a
            # bearer either way.
            kind = "bearer"
        fields = legacy.pop("fields", ()) or ()
        return {
            "kind": kind if kind in _AUTH_KINDS else "api_key",
            "credential": legacy.pop("credential", "") or "",
            "provider": legacy.pop("provider", "") or "",
            "scopes": tuple(legacy.pop("scopes", ()) or ()),
            "fields": tuple(
                f if isinstance(f, dict) else {"name": str(f)} for f in fields
            ),
            "setup_url": legacy.pop("setup_url", "") or "",
            "docs_url": legacy.pop("docs_url", "") or "",
        }


_AUTH_KINDS = frozenset({"none", "api_key", "basic", "bearer", "oauth2"})


class ToolsetManifest(BaseModel):
    """Declarative description of an external integration."""

    id: str
    """Unique identifier, e.g. ``salesforce``."""
    version: str
    """Semantic version of this manifest."""
    kind: ToolsetKind = ToolsetKind.APP
    """What sort of toolset this is — an app integration, an MCP server, a
    knowledge base, agent memory, or a skill. Lets an agent tell a first-party
    Jira toolset from an MCP-sourced one that exposes similar operations."""
    provider: str = ""
    """Who supplies these tools — e.g. ``loom``, ``mcp:atlassian``, ``acme-corp``.
    Together with ``kind`` and ``id`` this is what makes a toolset addressable
    when two of them describe the same underlying service."""
    summary: str
    """Tier 1 index card — short description for search results."""
    description: str = ""
    """Full description."""
    groups: dict[str, list[OperationSpec]] = Field(default_factory=dict)
    """Resource groups: ``group_name → [OperationSpec, ...]``."""
    auth: AuthSpec = Field(default_factory=AuthSpec)
    """How this toolset is authenticated — see :class:`AuthSpec`.

    Typed since the connection work: a free-form dict could not answer which
    OAuth provider serves this toolset, which credential key its client reads,
    or which of its environment variables is a secret — so nothing could tell
    a person what to connect, and ``loom connect jira`` did not work at all."""
    base_url: str = ""
    """Base URL for the API."""
    rate_limits: dict[str, Any] = Field(default_factory=dict)
    """Rate limit configuration per group or global."""
    egress_hosts: list[str] = Field(default_factory=list)
    """Declared egress hosts for sandbox enforcement."""
    fakes_module: str = ""
    """Python module path containing fake implementations for testing."""
    tools_module: str = ""
    """Importable module exposing this toolset's operations as ``@step`` functions.

    ``loom.toolsets.google.gmail.tools``, for example. This is what
    turns a manifest from a description into something generated code can call:
    documentation built from a manifest without it lists operation ids that
    exist in no namespace, and a model writing code against that guesses an
    import — plausibly, confidently, and wrongly."""
    opaque_ids: dict[str, str] = Field(default_factory=dict)
    """Regex for an identifier only this service can issue → the entity kind
    whose resolver produces it.

    The counterpart to :meth:`resolvers`. That says *how* to turn a person's
    word into an id; this says what such an id looks like once it is in the
    code, so a generated workflow containing ``customfield_10042`` can be
    checked against whether anything was ever resolved to produce it. A
    fabricated id is the failure entity resolution exists to prevent, arriving
    one step later: it validates, it runs against fakes, and in production it
    either 400s or writes to whichever field happens to hold that number.

    Only patterns nobody would type from knowledge belong here — a Jira
    ``customfield_10016`` or a Slack ``C024BE91L``, not an integer id that is
    indistinguishable from any other number. **An absent pattern is not a claim
    that a toolset's ids are safe to guess**, only that no pattern describes
    them precisely enough to check, and a check that flags ordinary data is one
    people switch off."""

    def resolvers(self) -> dict[str, OperationSpec]:
        """Entity kind → the operation that resolves it."""
        return {
            op.resolves: op for op in self.all_operations() if op.resolves
        }

    def paginated(self) -> list[OperationSpec]:
        """Operations whose result may be a page of something larger.

        The counterpart to :meth:`resolvers`, and there for the same reason:
        the manifest knows something the caller needs and cannot infer from a
        signature. ``max_results: int`` looks identical whether it caps a
        complete answer or truncates a partial one.
        """
        return [op for op in self.all_operations() if op.pagination]

    def import_line(self) -> str:
        """The import a workflow needs to call this toolset, or ``""``.

        Empty when the manifest declares no ``tools_module`` or no operation
        names a function, because a half-specified import is worse than none.
        """
        names = sorted({op.function for op in self.all_operations() if op.function})
        if not self.tools_module or not names:
            return ""
        return f"from {self.tools_module} import {', '.join(names)}"

    @property
    def qualified_id(self) -> str:
        """Fully-qualified identity: ``<kind>:<provider>:<id>``.

        Two toolsets may both call themselves ``jira``; only this tells them
        apart. Registries key on it so a second registration augments the
        catalog instead of silently replacing the first.
        """
        return f"{self.kind.value}:{self.provider or 'local'}:{self.id}"

    def all_operations(self) -> list[OperationSpec]:
        """Return all operations across all groups."""
        return [op for ops in self.groups.values() for op in ops]

    def required_scopes(self) -> tuple[str, ...]:
        """Every scope a connect flow must request for this toolset.

        The union of what the operations declare and the flow-only extras on
        :attr:`AuthSpec.scopes`. Derived rather than declared a second time:
        CERT-05 already requires a scope on every write and destructive
        operation of an ``oauth2`` toolset, so re-listing them here would be a
        second source of truth that drifts the first time somebody adds an
        operation. What ``AuthSpec.scopes`` carries is what no operation
        implies — ``offline_access`` is requested so a refresh token is issued
        at all, and no single call needs it.
        """
        scopes = {scope for op in self.all_operations() for scope in op.scopes}
        return tuple(sorted(scopes | set(self.auth.scopes)))

    def find_operation(self, op_id: str) -> OperationSpec | None:
        """Find an operation by its dotted id, or by its function name.

        Both, because this codebase teaches both and used to accept one. The
        generated docs say *"Resolve a project with jira_resolve_project"* and
        `OpMatch.import_line` renders the same name, because that is what
        generated *code* writes — while this matched only `projects.resolve`,
        so `call_read_operation("jira.jira_resolve_project")` answered "no
        operation" for the exact name the model had just been given.

        Ids are tried first: an id is the addressing scheme and a function name
        is the convenience, so a toolset that somehow had both spellings
        resolves to the one it declared.
        """
        by_function: OperationSpec | None = None
        for ops in self.groups.values():
            for op in ops:
                if op.id == op_id:
                    return op
                if by_function is None and op.function and op.function == op_id:
                    by_function = op
        return by_function
