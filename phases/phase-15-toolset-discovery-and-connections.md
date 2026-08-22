# Phase 15 — Toolset Discovery and Connections

*The agent could not see the catalogue, and could not have used it if it had.*

---

## 0. The report, and what it actually was

```
>  List tickets in jira that are passed due date in saas
  ⏺ search_toolsets("jira")        0.0s
  ⏺ search_operations("jira issue search")  0.0s
  ⏺ search_toolsets("issue")       0.0s
  ⏺ search_toolsets("")            0.0s
  … 30 tool calls, every one empty …
  ⏺ call_read_operation(op_path="list", arguments={})
```

Thirty tool calls, no code. The user's hunch — *"all toolsets global registry is
not exposed to loom agent"* — is correct, and it is the first of **six**
independent defects on this one path (five substantive, one paper cut). Each was reproduced against this working
tree before anything below was written.

The second half of the hunch — that a missing account should trigger an OAuth
setup flow rather than a dead end — is a real and separate capability. It is
designed here as the **Connection Plane**, but it is worth being explicit: the
Jira request would have failed *even with credentials already connected*,
because of defects #1, #2 and #5.

---

## 1. Evidence

Every finding below is reproduced, not inferred.

### D1 — The process-global toolset catalogue is empty on the authoring path

`register_available_toolsets()` (`src/loom/toolsets/registry.py:200`) is what
seeds the 27 shipped manifests. Its callers:

| Caller | Seeds? |
|---|---|
| `loom toolsets` / `loom toolset <id>` (`cli/commands.py:1445,1497`) | yes |
| `loom doctor` (`cli/doctor.py:283`) | yes |
| `loom mcp` (`mcp_server/server.py:281`, `mcp_server/tools.py:645`) | yes |
| MCP authoring tools (`mcp_server/authoring.py:140`) | yes |
| `scripts/run_eval.py:59` | yes |
| **`cli/targets.py::resolve()` — every `loom` command and the session** | **no** |

```
$ python -c "from loom.runtime.engine import Runtime; print(Runtime().toolsets.list_toolsets())"
[]
```

`LocalFacade._coding_agent` (`facade.py:1071`) passes
`tool_registry=self.runtime.toolsets`, which chains to that empty global. So
`loom author`, `loom edit` and the session see **zero** toolsets while
`loom mcp` sees **27**. One authoring implementation, two worlds.

This was deliberate once, and the reasoning is still sound
(`toolsets/registry.py:138-152`): eager registration makes
`resolve_tools(None)` — the no-ids sweep behind a prompt-only
`ctx.agent("summarise this")` — hand out `jira_delete_issue`. **The fix must
not undo that.** §3.1 keeps it.

### D2 — `search_operations` and `profile_of` do not chain to the parent

`ToolsetRegistry` implements parent delegation by overriding methods one at a
time: `get`, `effect_of`, `get_toolset`, `list_toolsets`, `search`, `show`,
`stub`. `ToolsetCatalog.search_operations` and `ToolsetCatalog.profile_of` were
added later and were never overridden, so they read `self._manifests` and stop.

```
child = ToolsetRegistry(parent=get_catalog())   # global seeded with all 27
child.list_toolsets()                  -> 27
child.search("jira")                   -> 1
child.search_operations("search issues") -> 0     # <-- silently empty
child.profile_of("jira_search_issues") -> None    # <-- silently absent
```

`search_operations` is the second tool the model reached for in the transcript.
Seeding the catalogue alone would have left that call returning `[]`.

**`profile_of` is the worse half, and the first draft of this plan called it
latent. It is not.** `runtime/context.py::_declared_effect` reads it on *every*
`ctx.step`, preferring it over `effect_of` and falling back only when the
attribute is **absent** — which it never is, since it is inherited. So the
chained `effect_of` was unreachable from a Runtime, and a toolset registered on
the process-global catalogue dispatched with no effect class at all:

```
register_available_toolsets()          # what `loom mcp` does at startup
get_catalog().profile_of("jira_search_issues")   -> EffectProfile(effect=READ, …)
Runtime().toolsets.profile_of(…)                 -> None      # <-- every run
```

Every toolset step in that process therefore reached the broker as an
unclassified **write** — defeating, one layer up, the lookup that exists to
stop exactly that. It is why the fix in §3.1 is structural rather than a third
override.

### D3 — With an empty catalogue, correct code is rejected as an error

`WorkflowCodingAgent._check_context` (`coding_agent.py:1108-1120`) sets
`available_toolsets = set(self._tool_registry.list_toolsets())` — an **empty
set**, not `None`. `None` disables the check; an empty set enables it against
nothing. `CodeValidator._check_toolsets` (`validator.py:219-232`) therefore
reports, for any `from loom.toolsets.jira.tools import ...`:

> this environment has no 'jira' toolset (available: none). Do not write code
> against an integration that is not configured — say the task cannot be done
> here instead.

Severity `error`, on the `static` stage, which is **blocking**. And
`_is_unrepairable` (`coding_agent.py:1801-1809`) returns `True` when every
issue category is `toolset`, so the job gives up rather than repairing. The
agent in the transcript was not merely unlucky; the path was closed at both
ends.

### D4 — Nothing maps a toolset to its OAuth provider, and most toolsets have no store path at all

```
$ loom connect jira
'jira' needs a token endpoint and a client id … 'jira' is not a known provider
```

`_resolve_target` (`cli/auth_commands.py:265-269`) looks the *credential name*
up in the OAuth provider registry. Jira's provider is `atlassian`. Gmail's is
`google_gmail`. Teams/OneDrive/SharePoint/OneNote/Outlook all sit behind
`microsoft`. Nothing declares any of that.

Worse, only **6 of 27** toolsets read a `CredentialStore` at all:

| Reads `resolve_bearer_token` | Credential name |
|---|---|
| `jira/client.py:173` | `jira` |
| `confluence/client.py` | `confluence` |
| `google/auth.py:165` | `google` |
| `microsoft/auth.py:178` | `microsoft` |
| `zoom/auth.py:122` | `zoom` |
| `slack/client.py` | `slack` (hard-coded literal) |

The other 21 — github, gitlab, stripe, salesforce, hubspot, airtable, asana,
clickup, quickbooks, exa, tavily, duckduckgo, and the five google/microsoft
sub-toolsets that route through the shared auth modules — are environment
variables only. `ToolsetManifest.auth` is `dict[str, Any]`
(`toolsets/manifest.py:191`) and its shape varies per toolset: some declare
`credential`, most declare only `fields`, one declares a `header`, one a
`grant`. There is no queryable answer to *"what does this toolset need, and
does this machine have it?"*

### D5 — Authoring-time reads cannot use a connected credential

`credential_store_scope` is bound at exactly one site:
`runtime/context.py:476`, inside `_attempt_loop`. `_call_read_operation`
(`agents/coding_tools.py:564`) calls `await fn(**arguments)` directly —
correctly, since authoring is not a durable execution — so
`current_credential_store()` is `None` and every toolset falls back to
environment variables.

So `loom connect jira` today stores a credential that the *runtime* can use and
the *authoring agent* cannot. The failure surfaces as
`agents/coding_tools.py:591-603`:

> If this is a credentials or network failure, the operation is fine — you
> simply cannot resolve it here. Generate code that resolves it at runtime
> instead.

Which is advice with no move behind it, and which pushes the model straight
past rung 2 of the resolution ladder into inventing an id.

### D6 (paper cut) — the docs teach a name the tool refuses

`ToolsetCatalog.describe` renders
`Resolve a project with jira_resolve_project` — the **function** name, because
that is what generated code writes. `OpMatch.import_line` does the same.
`ToolsetManifest.find_operation` (`manifest.py:269`) matches **only `op.id`**
(`projects.resolve`). The transcript shows the model doing exactly what it was
taught:

```
call_read_operation(op_path="jira.jira_resolve_project", …)
```

which, even with a seeded catalogue, answers *no operation
'jira_resolve_project'*.

---

## 2. Scope, and what is deliberately out

**In.** Making the shipped catalogue visible to every authoring surface without
widening what an unscoped `ctx.agent()` can reach; making "is this connected?"
a first-class, queryable property of a toolset; giving the authoring agent a
way to *get* a connection instead of a way to give up; and keeping every one of
those on the shared port so the CLI, the session and MCP get one
implementation.

**Out.**

- *Registering the OAuth app with the provider for you.* There is no
  `loom events install` for the same reason: owning N provider admin APIs
  forever, for a once-per-deployment act. The user creates the app; LOOM tells
  them the exact redirect URI and the exact scopes.
- *Multi-tenant / per-end-user connections.* This phase is the developer's own
  machine and the host's own service account. `AuthorizedFacade` gating is
  designed in; per-principal credential partitioning is not.
- *Dynamic Client Registration (RFC 7591).* Two of twelve built-in providers
  support it. Designed for (§4.3 leaves the seam), not implemented.

---

## 3. HLD

Two planes, deliberately separate, meeting only at one read-only query.

```
                       ┌──────────────────────────────────────────┐
                       │            AUTHORING SURFACES            │
                       │  loom author │ loom edit │ session │ MCP │
                       └────────────────────┬─────────────────────┘
                                            │ RuntimeFacade
                       ┌────────────────────┴─────────────────────┐
                       │                                          │
        ┌──────────────▼──────────────┐        ┌──────────────────▼─────────────┐
        │      DISCOVERY PLANE        │        │       CONNECTION PLANE         │
        │                             │        │                                │
        │  ChainedCatalog             │        │  ConnectionInspector  (read)   │
        │   ├ local registrations     │        │   └ status per toolset         │
        │   ├ process-global registry │◄───────┤  ConnectFlow          (write)  │
        │   └ BuiltinCatalog (lazy)   │ needs? │   ├ OAuthBrowserFlow           │
        │                             │        │   ├ OAuthDeviceFlow            │
        │  search / show / stub /     │        │   └ ApiKeyFlow                 │
        │  search_operations /        │        │  AppRegistrationStore          │
        │  describe / effect_of       │        │  CredentialStore (existing)    │
        └──────────────┬──────────────┘        └──────────────────┬─────────────┘
                       │                                          │
                       │  discovery scope                         │ execution scope
                       │  (what may be written against)           │ (what may be swept)
                       ▼                                          ▼
              CodeValidator allowlist                   resolve_tools(None)
              system-prompt roster                      ctx.agent() with no ids
```

### 3.1 Discovery scope ≠ execution scope

This is the whole of the D1 fix, and it is not new — `get_toolset`
(`tool_registry.py:377-392`) already draws exactly this line for *resolution*:

> A fallback, not a registration: the built-ins stay out of `list_toolsets` and
> so out of `resolve_tools`'s no-ids sweep. Asking for one by name gets it;
> asking for "everything" does not quietly acquire four integrations and their
> destructive operations.

The defect is that the same line was never drawn for *discovery*. So:

| Question | Method | Scope |
|---|---|---|
| what may an unscoped `ctx.agent()` sweep? | `list_toolsets()` | local + parent |
| what may generated code import and call? | `catalogue_ids()` **(new)** | local + parent + built-ins |
| what may the coding agent browse? | `search`/`show`/`stub`/`search_operations`/`get`/`describe` | catalogue |
| what may `toolsets=["jira"]` resolve? | `get_toolset()` | catalogue (unchanged) |
| what is this step's effect class? | `effect_of`/`profile_of` | **execution** — see below |

`effect_of` and `profile_of` are the exception, and it is deliberate. They are
the broker's per-dispatch lookup, and their answer decides what a run is
*allowed to do* — so widening them to the built-in tier would reclassify steps
in deployments that registered no toolset at all. Under `TaintBroker` that
turns an unclassified write into an open-world read, which changes which calls
are refused. That is a policy change, not a discovery one, and it ships
separately with its own tests. Step 1 chains them to the **parent**, which is
the live bug in D2, and stops there.

`resolve_tools(None)` keeps reading `list_toolsets()`. Nothing an agent gets
today it does not get after this. What changes is that a *name* — in a prompt
block, in a validator allowlist, in a search result — reaches the built-ins,
which is exactly what `get_toolset` already did.

### 3.2 Chaining becomes structural, not per-method

D2 is not a missing override; it is a design that requires an override per
method and therefore loses one every time a method is added. Two catalogue
methods have already been lost this way.

Replace the override chain with a **composite**: `ToolsetRegistry` holds an
ordered `tuple[ToolsetCatalog, ...]` of tiers and resolves every read across
them. Adding `search_operations` to the base class then chains for free (OCP);
adding the built-in tier is one list entry rather than seven overrides (DRY);
and a `ToolsetRegistry` is substitutable for a `ToolsetCatalog` in every method,
not six of nine (LSP).

### 3.3 A connection is declared, never guessed

`ToolsetManifest.auth` becomes a typed `AuthSpec` naming the credential, the
OAuth provider, the scopes and the environment fallback. The rule is the one
`OperationSpec.effect` already follows, and for the same reason: mapping `jira`
→ `atlassian` by string similarity is the guess `DEFAULT_SYSTEM_PROMPT` names
as the tell for a rule nobody should write. `gmail` → `google_gmail` and
`teams` → `microsoft` are not derivable from any rule; they are facts a
manifest states.

`tests/test_manifest_auth.py` asserts every shipped manifest declares one and
that every declared `provider` resolves in the OAuth provider registry — the
shape `tests/test_manifest_imports.py` already uses to stop a manifest
promising a symbol that is not there.

### 3.4 "Not connected" is an outcome, not an exception

An exception aborts the model's turn and teaches it nothing —
`mcp_server/` already states this rule for tool errors. Arcade's engine takes
the same position for authorization: a tool call that needs an account returns
an authorization request with a URL, and the caller completes it and retries.

So `call_read_operation` returns a **structured `not_connected` payload**
naming the toolset, what it needs, and the one tool that fixes it — and
`connect_toolset` is offered **only when the surface composed both a
`UserInteraction` and a `ConnectFlow`**. Absence omits the tool entirely rather
than offering one that answers *"not configured"*; that is the rule `ask_user`
and `observe_target` already follow, and it is what keeps a headless
`loom author --no-ask` in CI bit-for-bit what it is today.

### 3.5 A missing credential must never change the generated code

The hazard is specific and this repository has hit it three times
(`AutoRespondChannel`, `FakeBrowserProvider(permissive=True)`, the gutted
repair). If "not connected" reaches the model as an error, the cheapest repair
is to delete the integration — and the result passes every remaining stage.

So: `ConnectionStage` is a **warning**, never an error. `report.errors` drives
the repair loop; a warning does not reach it. The code is *correct*; the
machine is merely unconfigured, which is the host's business and not the file's.
The CLI prints `loom connect jira` in the summary instead.

This is the deliberate opposite of `BrowserEffectStage`, which errors on a
declared write with no approval — there, the run genuinely cannot reach the
write, so the code will not do what it says.

### 3.6 Where the code lives — the library / CLI boundary

`loom` and `loomsdk` are two console scripts on **one wheel**
(`pyproject.toml:190-192`), so this is not a packaging boundary that pip
enforces. It is a layering property, and today it holds exactly:

```
$ grep -rn "from loom\.cli" src/loom | grep -v "^src/loom/cli/"
src/loom/mcp_server/__main__.py:13:    from loom.cli import build_parser

$ grep -rn "rich|prompt_toolkit|textual|argparse" src/loom | grep -v "^src/loom/cli/"
src/loom/toolsets/google/setup.py:18: import argparse
```

One import edge, from a `__main__`; one `argparse`, in a module whose docstring
says *"Nothing here is imported by the toolsets."* Three tiers, and the middle
one is the part that is easy to get wrong:

| Tier | May depend on | This phase adds |
|---|---|---|
| **Library** | stdlib + declared deps. Never `loom.cli`, never a `[cli]` extra, never `argparse` | `AuthSpec`, `ConnectionInspector`, `AppRegistrationStore`, the `ConnectFlow` / `SecretPrompt` **protocols**, `ConnectEvent` |
| **Library adapters**, stdlib-only, **opted into by a host and never installed by import** | stdlib only; guarded by `available()` | `OAuthBrowserFlow`, `OAuthDeviceFlow`, `ApiKeyFlow`, `ConsoleSecretPrompt` |
| **CLI** | everything, `rich` / `prompt_toolkit` / `argparse` included | `cmd_connect`, `loom connections`, rendering a `ConnectEvent` as a line, composing the adapters into `LocalFacade` |

The middle tier is not a compromise; it is the position `CLIUserInteraction`
already occupies — a stdin-reading, stderr-writing adapter that sits in
`agents/interaction.py` while `PromptUserInteraction`, which needs
`prompt_toolkit`, sits in `cli/repl/`. The dividing line is **the extra, not
the terminal**. And `LocalFacade.user_interaction`'s own docstring states the
rule this phase must obey:

> a library that reads stdin because it was imported is the ambient behaviour
> `Runtime` avoids everywhere else: the CLI opts in, a server does not.

So: a browser is opened and a secret is read **only because a host passed the
adapter in**. `loom.connectors` importable does not mean `webbrowser.open` is
reachable, any more than importing `loom.agents.interaction` reads stdin today.

Two consequences worth stating, because the first draft of this plan got both
wrong by omission:

- **A flow may not take a `Printer`.** `_run_pkce_flow` currently writes four
  lines through `loom.cli.output.Printer` (`auth_commands.py:502-506`). Lifting
  it as-is would put a `[cli]`-flavoured renderer inside `loom/connectors/` and
  create the second library→CLI edge. §4.4 replaces it with an event.
- **`pip install loomsdk` — core, no extras — gains nothing.** `ApiKeyFlow`,
  `OAuthBrowserFlow` and `MemoryCredentialStore` are stdlib plus `httpx`, which
  the toolsets already require; `KeyringCredentialStore` needs `[credentials]`
  and already did. No dependency moves tiers.

---

## 4. LLD

### 4.1 `loom/toolsets/catalog.py` — composite chaining

```python
class ToolsetCatalog:
    """Unchanged: a flat store of manifests over `self._manifests`."""
```

```python
# loom/agents/tool_registry.py

class ToolsetRegistry(ToolsetCatalog):
    def __init__(
        self,
        parent: ToolsetCatalog | None = None,
        *,
        allow_builtin_fallback: bool = True,
    ) -> None:
        super().__init__()
        self._toolsets: dict[str, Toolset] = {}
        self._parent = parent
        self._allow_builtin_fallback = allow_builtin_fallback

    # -- the two scopes ---------------------------------------------------

    def _execution_tiers(self) -> tuple[ToolsetCatalog, ...]:
        """What a no-ids sweep may reach: what somebody registered."""
        return (self, *( (self._parent,) if self._parent is not None else () ))

    def _catalogue_tiers(self) -> tuple[ToolsetCatalog, ...]:
        """What may be *named*: registrations, then the toolsets LOOM ships.

        The built-in tier is last so a host registering its own `jira` shadows
        it, and it is lazy — `BuiltinToolsetCatalog` imports a manifest module
        on first read and no `tools` module ever.
        """
        tiers = self._execution_tiers()
        if self._allow_builtin_fallback:
            tiers += (builtin_catalog(),)
        return tiers

    # -- reads, resolved across tiers -------------------------------------

    def get(self, toolset_id): return _first(self._catalogue_tiers(), "get", toolset_id)
    def show(self, tid, group=None): ...
    def stub(self, op_path): ...
    def effect_of(self, function): ...
    def profile_of(self, function): ...          # chains now, by construction
    def search(self, query, *, limit=10): ...     # merged, de-duped, nearest first
    def search_operations(self, q, **kw): ...     # chains now, by construction

    def list_toolsets(self) -> list[str]:
        """Execution scope. Unchanged semantics — `resolve_tools` reads this."""

    def catalogue_ids(self) -> list[str]:
        """Discovery scope: everything that may be named."""
```

Three helpers (`_first`, `_merge`, `_union`) carry the whole chain, so a new
catalogue method needs one line and cannot silently stop at tier 0.

`get_toolset` keeps its existing behaviour and its existing docstring — the
built-in fallback there is now the *executable* half of the same tier list.

**`BuiltinToolsetCatalog`** (`toolsets/registry.py`) is a `ToolsetCatalog`
subclass whose `_manifests` is populated lazily on first read from
`BUILTIN_TOOLSETS`. Measured: 0.48 s for all 27 manifests, once per process, no
`httpx`, no vendor SDK. It is a process singleton (`builtin_catalog()`), so a
Runtime costs nothing extra.

`register_available_toolsets()` stays exactly as it is. It remains the right
call for `loom mcp`, whose job *is* to publish an integration surface, and for
`loom toolsets`, which reports what a process can reach. What it stops being is
the only path to discovery.

### 4.2 `AuthSpec` — a toolset says what it needs

```python
# loom/toolsets/manifest.py

class AuthField(BaseModel):
    """One environment variable a toolset reads when nothing is connected."""
    name: str                     # "JIRA_API_TOKEN"
    label: str = ""               # "Atlassian API token"
    secret: bool = True           # never echoed, never recorded
    required: bool = True
    example: str = ""


class AuthSpec(BaseModel):
    """How a toolset is authenticated. Declared, never inferred."""
    kind: Literal["none", "api_key", "basic", "bearer", "oauth2"] = "none"

    credential: str = ""
    """The `CredentialStore` key this toolset's client reads.

    The single fact that was nowhere: `jira/client.py` defaults
    `credential_name="jira"` and nothing outside that file could learn it.
    """

    provider: str = ""
    """`OAuthProviderConfig.id`, when `kind == "oauth2"`.

    Declared because it is not derivable: `jira` -> `atlassian`,
    `gmail` -> `google_gmail`, `teams` -> `microsoft`.
    """

    scopes: tuple[str, ...] = ()
    """What *this toolset* needs — narrower than the provider's defaults.

    A Jira-only workflow should not be asked to grant Confluence.
    """

    fields: tuple[AuthField, ...] = ()
    setup_url: str = ""        # where a person creates the app / key
    docs_url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_dict(cls, value): ...
```

`ToolsetManifest.auth` keeps its name and its position; its annotation becomes
`AuthSpec`, and a `mode="before"` validator promotes the legacy
`{"type": ..., "fields": [...]}` dict so nothing third-party breaks. The 27
shipped manifests are migrated in the same change — that migration *is* the
work of §6 step 3, because the mapping is 27 hand-checked facts and no rule.

Jira, worked:

```python
auth=AuthSpec(
    kind="oauth2",
    credential="jira",
    provider="atlassian",
    scopes=("read:jira-work", "write:jira-work", "offline_access"),
    fields=(
        AuthField(name="JIRA_URL", label="Site URL", secret=False,
                  example="https://acme.atlassian.net"),
        AuthField(name="JIRA_EMAIL", label="Account email", secret=False),
        AuthField(name="JIRA_API_TOKEN", label="API token"),
    ),
    setup_url="https://developer.atlassian.com/console/myapps/",
    docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
)
```

Both routes are declared because both work and they are not alternatives to
each other: OAuth is the connected path, the fields are the fallback the client
already has (`jira/client.py:146-160`).

### 4.3 `ConnectionInspector` — the read-only query

```python
# loom/connectors/inspect.py  (new)

class ConnectionState(StrEnum):
    CONNECTED = "connected"   # a usable credential exists
    DUE       = "due"         # usable, renewal is imminent
    EXPIRED   = "expired"     # present and past expiry
    ENV       = "env"         # no stored credential; env vars satisfy it
    MISSING   = "missing"     # nothing
    NONE      = "none"        # the toolset needs no credential


class ConnectionStatus(BaseModel):
    toolset: str
    state: ConnectionState
    method: Literal["oauth", "api_key", "basic", "bearer", "none"]
    credential: str = ""
    provider: str = ""
    scopes: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    expires_at: datetime | None = None
    setup_url: str = ""
    how: str = ""      # the exact command: "loom connect jira"


class ConnectionInspector:
    """Answers 'is this connected?' from manifests, a store, and the env.

    Layer 1 discipline: reads `AuthSpec` and `CredentialStore.peek`, imports no
    vendor module, opens no socket, and never mints a token. Safe to call on
    every authoring job and every `loom doctor`.
    """
    def __init__(self, catalog, store=None, *, environ=None, clock=None): ...
    async def status(self, toolset_id: str) -> ConnectionStatus: ...
    async def all(self) -> list[ConnectionStatus]: ...
```

Three states rather than two for the stored case, following `loom whoami`
(`auth_commands.py:646-651`): `due` is the one worth surfacing, because
repeated `due` means renewal is failing long before anything breaks.

`ENV` is separate from `CONNECTED` on purpose. A toolset satisfied by
`JIRA_API_TOKEN` in `.env` needs no OAuth flow, and offering one would be the
`loom refresh --all` failure — advice for a state the machine is not in.

### 4.4 `ConnectFlow` — the write, extracted from argparse

Today the entire OAuth flow lives inside `cli/auth_commands.py` and takes an
`argparse.Namespace` (`_resolve_target(args, …)`, `_connect(args, out, …)`).
Nothing that is not argparse can perform a connection. That is the DRY problem
under the user's request: the session, MCP and the agent each need it, and none
of them has a `Namespace`.

```python
# loom/connectors/flows.py  (new)

@dataclass(frozen=True)
class ConnectRequest:
    name: str                       # credential name, e.g. "jira"
    provider: str = ""              # OAuthProviderConfig id
    scopes: tuple[str, ...] = ()
    client_id: str = ""
    client_secret: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)   # api-key route
    redirect_port: int | None = None
    timeout: float = 300.0


@dataclass(frozen=True)
class ConnectOutcome:
    connected: bool
    name: str
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    needs: tuple[AuthField, ...] = ()   # what is still missing
    redirect_uri: str = ""              # to register, when that is the blocker
    authorization_url: str = ""         # printed when a browser cannot open
    reason: str = ""


@dataclass(frozen=True)
class ConnectEvent:
    """Something a person may want to see while a flow runs.

    A structured event, never a rendered line. `_run_pkce_flow` takes a
    `loom.cli.output.Printer` today; carrying that into `loom/connectors/`
    would put a `[cli]`-flavoured renderer in the library and add the second
    library→CLI import edge in the codebase (§3.6).

    Same shape and same reason as `LocalFacade.on_stage`, and the same
    arrangement as `ProgressRenderer`, which is an ordinary consumer of seams
    the agent layer knows nothing about.
    """
    kind: Literal["needs_app", "redirect_ready", "opening_browser",
                  "waiting", "exchanged", "stored"]
    redirect_uri: str = ""
    authorization_url: str = ""
    scopes: tuple[str, ...] = ()
    setup_url: str = ""
    detail: str = ""


class SecretPrompt(Protocol):
    """How a secret field is collected. Never `ask_user` — see §8."""
    def available(self) -> bool: ...
    async def read(self, field: AuthField) -> str: ...


class ConnectFlow(Protocol):
    def supports(self, spec: AuthSpec) -> bool: ...
    async def connect(
        self,
        request: ConnectRequest,
        spec: AuthSpec,
        *,
        on_event: Callable[[ConnectEvent], None] | None = None,
    ) -> ConnectOutcome: ...
```

`on_event` is optional and fails open: a flow with no listener still connects,
and a renderer that raises must never fail a connection that succeeded — the
rule the non-deciding hook families already follow.

Three implementations, each a straight lift of code that already works:

- **`OAuthBrowserFlow`** — `_PkceListener` + `_run_pkce_flow`
  (`auth_commands.py:471-527`), with the four `out.line(...)` calls replaced by
  `on_event(...)` and the `Printer` parameter dropped. PKCE by default, which
  is RFC 8252's requirement for a native client on a loopback redirect. Stdlib
  plus `httpx`; a middle-tier adapter, so importing it opens nothing.
- **`OAuthDeviceFlow`** — `_run_device_flow`, same treatment, for a headless
  host.
- **`ApiKeyFlow`** — new, ~40 lines. It **does not prompt**: it reads
  `request.fields`, and reports any `AuthSpec.field` still missing as
  `ConnectOutcome.needs`. Collecting the missing ones is the caller's job,
  through `SecretPrompt` for the secret ones — so the flow stays a pure
  function of its request and the terminal stays in the tier that owns it.
  This is what makes the 21 env-only toolsets connectable at all.
- **`ConsoleSecretPrompt`** — `getpass` behind `available() -> sys.stdin.isatty()`,
  beside `CLIUserInteraction`'s precedent. Never constructed by the library.

`cli/auth_commands.py` keeps `cmd_connect`/`cmd_login` and becomes a thin
adapter: it reads flags, constructs the adapters, renders each `ConnectEvent`
as a `Printer` line, and renders the outcome — the shape `cmd_author` already
has over `facade.author`, and it decides nothing.

**`AppRegistrationStore`.** `client_id`/`client_secret` are per *provider* and
per machine, and today must be re-supplied by flag or env on every
`loom connect`. They are stored in the same `CredentialStore`, under the
reserved key `oauth-app:<provider>`, so they inherit its encryption at rest —
n8n's position, and the reason it is not `.env`. RFC 8252 is explicit that a
native app cannot treat a distributed secret as confidential; this is not
protecting the secret from the provider's threat model, it is keeping it out of
a git-tracked file and out of `ps`.

**Secrets never travel through `ask_user`.** `AskedQuestion` records the
question *and its answer* on `CodingResult.questions`, and `loom author
--save-answers` writes that to disk. A client secret collected through
`ask_user` would be written to a build-input file. So `ConnectFlow` collects
secret fields through a `SecretPrompt` seam (`getpass` on a TTY, MCP
elicitation with `"format": "password"` on a server), and `ConnectOutcome`
carries names and expiries only — never a token, exactly as
`cmd_refresh`'s JSON already does.

### 4.5 The facade — one implementation, four surfaces

```python
class RuntimeFacade(Protocol):
    async def connections(self) -> list[dict[str, Any]]: ...
    async def connect(self, toolset: str, *, client_id: str = "",
                      device: bool = False, scopes: list[str] | None = None,
                      ) -> dict[str, Any]: ...
    async def disconnect(self, name: str) -> dict[str, Any]: ...
```

**The adapters are constructor seams on `LocalFacade`, not defaults.**

```python
@dataclass
class LocalFacade:
    runtime: Runtime
    loaded: list[str] = field(default_factory=list)
    user_interaction: Any = None
    connect_flow: Any = None     # NEW — a ConnectFlow
    secret_prompt: Any = None    # NEW — a SecretPrompt
```

Both default to `None`, and `cli/targets.py::resolve()` composes them exactly
where it already composes `_interaction()` — which is the one place in this
codebase that knows there is a person at the other end of stdin. A host
embedding `LocalFacade` gets no browser and no `getpass` unless it asks, and
`RemoteFacade` cannot carry either because they are objects rather than
payloads: the same argument that put `user_interaction` here rather than on the
port.

Absence degrades the way `observe_target` and `ask_user` already do:
`connect_toolset` is not offered to the model at all, and `facade.connect()`
returns `ConnectOutcome(connected=False, needs=…, redirect_uri=…)` — the
information needed to connect it by hand — rather than blocking on a prompt
nobody can answer. That is also what makes `--json`, a pipe, and CI correct for
free, with no `isatty` check written anywhere in this phase.

`RemoteFacade.connect` **refuses, with the reason** — the position
`author` already takes. A connection is a browser on *this* machine writing a
keyring on *this* machine; doing it against a server would either spend the
server's OAuth app or store a token in the wrong keyring.

`AuthorizedFacade` gates `connect`/`disconnect` on a new
`credentials:connect` scope. Not `workflows:author`: authoring spends tokens
and reaches out, and neither implies being trusted to mint a credential the
whole process will then use. `connections()` (read-only, no secrets) rides on
the existing read scope.

### 4.6 What the agent sees

**Prompt (Tier 0 — a roster, not index cards).** `describe(detail="index")`
renders every operation id of every toolset: 1 738 characters per toolset, and
**33 699 characters ≈ 8 400 tokens** for 27 — against a docstring that promises
"~40 tokens each". That is a real regression risk of fixing D1 naively.

`detail="roster"` (new) is one line per toolset plus its connection state:

```
## Available toolsets

27 integrations are installed. `search_toolsets` and `search_operations` find
the right one; `show_toolset` then `get_tool_contract` give the exact call.

  jira      [connected]  Jira issue tracker — search, create, update, transition, comment.
  slack     [connected]  Slack — read and post messages, threads, channels, files.
  github    [not connected: loom connect github]  GitHub — repos, issues, PRs, search.
  …
```

Measured at **2 257 characters ≈ 565 tokens** for all 27, and it grows by ~20
tokens per integration rather than ~430. `detail="index"` is untouched for
`loom toolsets` and the MCP surface, where a person or a client is reading it.

Two rules travel with the roster, and the second is load-bearing:

> A toolset marked *not connected* is still real. Write the code you would
> write anyway; the person running it connects the account. Never change the
> design of a workflow because a credential is missing, and never say a task
> cannot be done here for that reason.

**Tools.** One new coding tool, offered conditionally:

```python
@tool
async def connect_toolset(toolset_id: str) -> str:
    """Connect an account so this toolset can be called.

    Use it when a lookup came back `not_connected` and you need the real data —
    an id, a status vocabulary, whether a project exists. It opens a browser on
    the user's machine and returns when they finish.

    You do not need this to *write* code against a toolset.
    """
```

Offered only when `build_coding_tools` receives both a `UserInteraction` that
`available()` and a `ConnectFlow`. `ConnectGate` bounds it the way `AskUserGate`
bounds asking: at most **2** connect attempts per job, counted in *attempts*
not calls, and switched off before repair and smoke so CI cannot deadlock.

**The `not_connected` payload** replaces the generic `except Exception` note in
`_call_read_operation` for exactly the credential exceptions
(`CredentialNotFound`, `AuthExpired`, and a toolset's own auth error class):

```json
{
  "error": "not_connected",
  "toolset": "jira",
  "needs": {"method": "oauth", "provider": "atlassian",
            "scopes": ["read:jira-work"]},
  "next": "connect_toolset(\"jira\")",
  "note": "You can also write the workflow without resolving this — say in the plan that the id is resolved at run time. Do not change what the workflow does because of this."
}
```

The last sentence is there because of §3.5.

**And the store is bound.** `_call_read_operation` wraps its invocation in
`credential_store_scope(self._credentials)`, closing D5:

```python
with credential_store_scope(credentials):
    result = await fn(**arguments)
```

`credentials` is threaded from `LocalFacade` (which already builds it —
`targets.py:181`) through `WorkflowCodingAgent` into `build_coding_tools`.
Rung 2 of the resolution ladder only exists once this is true.

### 4.7 `ConnectionStage`

| | |
|---|---|
| id | `connections` |
| cost | 14 (after `grants` at 12, before `coverage` at 15) |
| blocking | no |
| severity | **warning**, always |

Reads the imports of the generated file, maps each `loom.toolsets.*` module
back to a toolset id via `CheckContext.toolset_modules`, and asks the
`ConnectionInspector`. Emits one warning per unconnected toolset carrying the
exact command.

The severity is asserted **in both directions**, the discipline
`BrowserEffectStage` established: a test that it warns on unconnected, and a
test that it says *nothing* on connected or env-satisfied — because a stage
that fires on correct code is worse than no stage.

### 4.8 Paper cuts

- **D6.** `ToolsetManifest.find_operation` accepts an operation id *or* a
  function name, since `describe`, `OpMatch.import_line` and the resolution
  ladder all print the function name. One extra loop; the alternative is
  teaching two vocabularies and refusing the one we taught.
- **`describe()` on an empty catalogue** returns a sentence saying so rather
  than `""`. `DEFAULT_SYSTEM_PROMPT` says *"Only the toolsets listed above
  exist"*, and with `""` that referred to nothing at all — which is how the
  transcript's agent spent thirty turns looking for a list it had been told
  existed.
- **`_resolve_target`'s refusal** names the mapped provider when a manifest
  declares one: *"'jira' is served by the 'atlassian' provider"* rather than
  *"'jira' is not a known provider"*.

---

## 5. Data flows

### 5.1 Discovery — `loom author "list overdue jira tickets in saas"`

```
loom author ──► targets.resolve() ──► Runtime(store, credentials=KeyringStore)
                                          │
                                          └─ rt.toolsets = ToolsetRegistry(parent=get_catalog())
                                                              tiers: [local, global, BUILTIN]
                                                                                    ▲ NEW
LocalFacade.author
  └─ _coding_agent(tool_registry=rt.toolsets, credentials=rt.credentials)
       │                                                       ▲ NEW
       ├─ build_system_prompt()
       │    └─ describe(detail="roster")  ── 27 lines, ~565 tokens
       │         └─ ConnectionInspector.all()   (manifests + keyring peek; no network)
       │
       ├─ ReAct turn 1: search_toolsets("jira")
       │    └─ tier 0 miss → tier 1 miss → BUILTIN hit → [jira]        (D1)
       │
       ├─ turn 2: search_operations("overdue issues")
       │    └─ chained across tiers → [jira.issues.search, …]          (D2)
       │
       ├─ turn 3: get_tool_contract("jira.issues.search")
       │
       ├─ turn 4: call_read_operation("jira.jira_resolve_project", {"project_name": "saas"})
       │    ├─ find_operation accepts the function name                 (D6)
       │    └─ with credential_store_scope(keyring): jira_resolve_project(...)   (D5)
       │         ├─ connected  → {"key": "PA", "name": "Launch SAAS"}
       │         └─ missing    → {"error": "not_connected", "next": "connect_toolset(\"jira\")"}
       │
       └─ final_output(code, plan)
            └─ CheckPipeline
                 ├─ static: available_toolsets = catalogue_ids() → 27, import passes  (D3)
                 ├─ connections: warn iff unconnected — never an error       (§3.5)
                 └─ smoke: fakes installed from every catalogue manifest (0.13 s)
```

### 5.2 Connection — the agent needs the real project key

```
 agent          connect_toolset      ConnectFlow        SecretPrompt      provider     store
   │                   │                  │                  │               │           │
   │ connect_toolset("jira")              │                  │               │           │
   ├──────────────────►│                  │                  │               │           │
   │                   │ inspector.status("jira") = MISSING, oauth/atlassian │           │
   │                   ├─────────────────►│                  │               │           │
   │                   │                  │ app registration for "atlassian"?│           │
   │                   │                  ├──────────────────────────────────────────────►
   │                   │                  │◄─────────────── none ────────────────────────┤
   │                   │                  │                  │               │           │
   │        ┌──────────┴──────────────────┴──────────────────┴───────────────┴───────────┐
   │        │  PRINTED BEFORE ANYTHING IS ASKED FOR (n8n's read-only callback field):    │
   │        │    Redirect URI : http://127.0.0.1:8931/callback                           │
   │        │    Scopes       : read:jira-work offline_access                            │
   │        │    Create an app: https://developer.atlassian.com/console/myapps/          │
   │        └──────────┬──────────────────┬──────────────────┬───────────────┬───────────┘
   │                   │                  │ client_id  (visible prompt)      │           │
   │                   │                  ├─────────────────►│               │           │
   │                   │                  │ client_secret (getpass — never echoed,       │
   │                   │                  │                never recorded in questions)  │
   │                   │                  ├─────────────────►│               │           │
   │                   │                  │ put("oauth-app:atlassian")       │           │
   │                   │                  ├──────────────────────────────────────────────►
   │                   │                  │                  │               │           │
   │                   │                  │ listen 127.0.0.1:8931; PKCE S256 │           │
   │                   │                  │ webbrowser.open(authorization_url)           │
   │                   │                  ├──────────────────────────────────►           │
   │                   │                  │◄──── ?code=…&state=… (state checked) ────────┤
   │                   │                  │ exchange_code(code, verifier)    │           │
   │                   │                  ├──────────────────────────────────►           │
   │                   │                  │◄──── access + refresh token ─────┤           │
   │                   │                  │ put("jira", StoredCredential + refresher metadata)
   │                   │                  ├──────────────────────────────────────────────►
   │◄──────────────────┤ {"connected": true, "scopes": [...], "expires_at": ...}         │
   │  (no token, ever) │                  │                  │               │           │
   │ retry: call_read_operation("jira.jira_resolve_project", {"project_name": "saas"})   │
   │  → {"key": "PA", "name": "Launch SAAS"}                                             │
```

Interruption is safe at every arrow: nothing is written until the exchange
succeeds, and `MetadataRefresher` metadata is stamped on the same write
(`auth_commands.py:566`) so the credential can renew itself later with no CLI
in the picture.

### 5.3 Run time — unchanged

```
loom run overdue --input '{"project":"PA"}'
  └─ engine._attempt_loop
       └─ credential_store_scope(rt.credentials)        ← already exists (context.py:476)
            └─ jira_search_issues(...)
                 └─ resolve_bearer_token("jira")        ← AuthSpec.credential
                      ├─ present, fresh   → Bearer
                      ├─ present, due     → refresh, then Bearer     (RefreshPolicy)
                      ├─ present, expired → AuthExpired → run parks on credential:jira
                      └─ absent           → JIRA_EMAIL/JIRA_API_TOKEN → Basic
```

Nothing in this plane changes. That is the point: authoring and running now
read the *same* credential by the *same* name, and the name is declared in one
place instead of being a default argument in one client file.

---

## 5a. Describe a task, and have it happen

Out of the original nine steps, and added because the reported failure was
never only discovery. `loom author` wrote a file, registered it, printed a
summary and stopped — so *"find my jira tickets"*, a question whose answer is
the tickets, produced Python and the sentence `loom run my_jira_tickets`. The
task **was** the query.

**It is the same defect as D1, three more times.** A grep across
`src/loom/agents/` for `triggers=`, `Schedule`, `OnAppEvent` or `@pure`
returned **nothing**. `@workflow(triggers=[...])` and the four step classes
had shipped long ago; the model had never been told either existed, so every
generated workflow was implicitly `Manual` and every helper implicitly an
effect. Capability present, nobody wired to it — exactly what
`tests/test_ask_user_wiring.py` was written for.

**Two decisions, deliberately independent.** Conflating them is what makes
either unpredictable:

| Question | Answered by |
|---|---|
| what gets *wired* | the triggers the workflow declares. A question declares none and runs once, now; "every weekday at 9" declares a `Schedule`. |
| whether the immediate run *asks first* | `loom/agents/impact.py`, from the effect classes the **manifests** declare |

A declared schedule does not suppress the first run: seeing it work once is
most of the reason to have asked for it, and a workflow whose first execution
is at 9am tomorrow is one nobody has tested.

**`impact_of` never asks the model what its own code does.** It reads the
durable calls out of the AST and the effect class out of the manifest — the
position `IdentifierStage` takes about resolved ids, because a self-report
certifies precisely the case the check exists to catch.

Three properties, each the fix for something specific:

- **Undeclared is an effect, not a read.** `OperationSpec.effect` defaults to
  WRITE for the same reason: guessing "read" wrong issues a refund nobody was
  asked about, and guessing "write" wrong costs one keystroke. `@pure` is how
  an author says a step reaches nothing — a declaration, not an inference
  drawn from the shape of a function body. Without it every read-only workflow
  would ask, because they all end in a formatting step.
- **An approval is not a write, and neither is `ctx.agent`.** Counting the
  first would mean a workflow with a human gate needs a second one to reach the
  first; counting the second would make every judgement step need confirming.
- **It reads the catalogue, not `effect_of`.** That lookup is the broker's
  per-dispatch one and is deliberately execution scope (§3.1) — it answers
  `None` for Jira on a bare Runtime. This is a declaration being read back to a
  person before they press a key, so it resolves through the file's own
  imports: `from loom.toolsets.jira.tools import …` names the toolset exactly.

**Non-interactive refuses**, the rule `before` hooks and `propose` already
follow: a gate that could not run has not passed. `--yes` is the override and
the refusal names it.

**`--run` is on in the session and off in `loom author`.** A line typed at the
session prompt is a task; `loom author "spec" > flow.py` is a documented shape
and a run would put its output in the file.

**Acting is awaited, never driven.** The first version called `run_async` from
inside `_act_on` — which is already inside the loop `run_async` opened for
`cmd_author` — so every invocation raised *"asyncio.run() cannot be called from
a running event loop"* **after** the file was written and the summary printed,
which is the worst place to fail. Every unit test passed: they called
`_may_run` directly and nothing called the command. `tests/test_author_and_act.py`
now drives `cmd_author` itself with a fake facade, which is the only shape that
catches it, and asserts none of the three functions contains `run_async(`.
Awaiting also keeps the run inside the same `guarded` scope, so Ctrl+C settles
its lease exactly as it does for `loom run`; and the confirmation prompt reads
stdin through `asyncio.to_thread`, because `input()` on the loop blocks every
timer and heartbeat the Runtime has running behind it.

*Status: done.* One thing worth recording. The prompt has a length budget whose
own docstring says *"the margin is one sentence wide on purpose, so the next
addition has to run this search too"* — so the search was run. It found one
real thing, and it was structural: both additions had written themselves new
`###` sections when they belonged in sections that already existed. `@pure` is
a step class, so it is a bullet beside `@step`; `triggers=` is written on the
decorator, so it is a bullet beside `@workflow(name=…)`. Folding them in
removed two headings and the sentence each needed to reintroduce its subject —
**55 characters**, which is the honest size of the redundancy that was there.
The rest is new information with nothing to merge it into, so the ceiling moved
11100 → 11900 with that reasoning written into the test, as the MCP schema
budget did at 18k → 24k.

---

## 6. Implementation plan

Ordered so each step is independently shippable and independently revertable.
Steps 1–2 alone fix the reported bug.

**1 — Chain structurally, and add the built-in discovery tier.** (D1, D2)
`toolsets/catalog.py`, `agents/tool_registry.py`, `toolsets/registry.py`.
Introduce `BuiltinToolsetCatalog` + `builtin_catalog()`; replace the seven
per-method overrides with `_catalogue_tiers()`/`_execution_tiers()` and the
three merge helpers; add `catalogue_ids()`. `list_toolsets()` and
`resolve_tools()` keep their exact current semantics.
*Test:* `tests/test_toolset_discovery.py` — `resolve_tools(None)` on a bare
`Runtime` still returns `[]`; a chained registry answers `search_operations`
and `profile_of`; a locally registered `jira` shadows the built-in *totally*,
including a missing operation; `allow_builtin_fallback=False` closes discovery
as well as resolution; and a **meta-test** enumerates every public read on
`ToolsetCatalog` and fails when one is added without a chaining case — the
discipline `BUILTIN_TOOL_DOCS` already uses, and the only kind of test that
would have caught D2, since the defect is precisely a method nobody remembered.

*Status: done.* Three things surfaced while implementing it, all recorded
above or below: D2's severity (`profile_of` is live, not latent); the
`effect_of`/`profile_of` scope carve-out; and a pre-existing hole in
`tests/conftest.py::isolated_catalog`, which restored the global catalogue's
`_manifests` and left the derived `_by_function_op` index holding 358 entries —
so a test that registered toolsets leaked their effect classification into
every test after it. `ToolsetCatalog.invalidate()` is now the one place that
drops those indexes, called by `register`, `unregister` and the fixture.

**2 — Point discovery at the catalogue scope.** (D3)
`coding_agent._check_context` and `_toolset_modules` read `catalogue_ids()`
through one `_catalogue()` helper that falls back for a host registry written
before the split; `build_system_prompt` calls a new
`ToolsetRegistry.prompt_block()`.
*Test:* generating against a bare `Runtime` no longer produces a `toolset`
error for a valid import, while an invented one is still refused; the roster is
asserted under 1 500 tokens for the shipped 27 and under a quarter of the index
for the same set; and it is asserted to grow *by exactly one line* when a 28th
is registered — line-count equality, the way `NodeRegistry.prompt_block` is
already pinned.

*Status: done, with one structural change to the plan.* `describe(detail=…)`
was going to gain a `roster` tier and switch its default to catalogue scope.
That broke seven existing tests, and they were right to break: `describe()`
documents *a given set of toolsets*, and a host asking for its own docs does
not mean "and also the 27 LOOM ships". So the roster is
**`ToolsetRegistry.prompt_block()`** instead — catalogue scope, never empty,
the direct counterpart of `NodeRegistry.prompt_block()`, which
`build_system_prompt` was already calling one block further down. `describe()`
is untouched: execution scope, `""` when there is nothing to describe.

Two costs are worth recording. The prompt no longer carries operation *names*
— an index card lists every one, which is what makes it grow with the size of
an integration rather than its existence — so `show_toolset` and
`get_tool_contract` now answer for them, which is what the three-tier catalogue
was for. And each roster line carries the **module**, because
`google_calendar` lives at `loom.toolsets.google.calendar` and an import built
from the id resolves as a plausible path and fails at run time. Measured on the
27 shipped: **1 268 tokens against 8 424**.

**3 — `AuthSpec`, and migrate the 27 manifests.** (D4, part 1)
`toolsets/manifest.py` + every `manifest.py`. Legacy-dict validator so
third-party manifests keep working.
*Test:* `tests/test_manifest_auth.py` — every shipped manifest declares an
`AuthSpec`; every `provider` resolves in `list_oauth_providers()`; every
`credential` matches a `credential_name` the client source actually reads, **in
both directions**; and no `provider` without a `credential`.

*Status: done.* Four things worth recording.

**The last rule is now enforced by the model, not just tested.** `provider`
without `credential` is refused at construction: the flow would open a browser,
store a token, print "Connected", and every call would still 401 against an
environment variable nobody set. It is a real shape — `github` and `hubspot`
both have a provider in the registry and clients that read no `CredentialStore`
— so both declare neither, and giving them a store path is a change to the
client rather than to a manifest.

**`credential=""` is a statement.** 12 of the 27 read environment variables and
nothing else; the other 15 share **6** credential names, because the five
Google toolsets share one token and the six Graph ones share another. The set
is pinned so growing it is deliberate.

**Scopes are derived, not declared twice.** `ToolsetManifest.required_scopes()`
is the union of the operations' own scopes and the flow-only extras on
`AuthSpec.scopes` — CERT-05 already requires a scope on every write, so
re-listing them would be a second source of truth. Writing the test found that
**Jira and Confluence declared no scope on any of their 21 reads**, alone among
the 17 OAuth toolsets, so a connect flow built on this would have requested a
write-only token. Fixed on the operations, where the other 15 already had it.

**The migration itself lost four variables**, and only one toolset had a test
that noticed: rewriting the `auth` literals dropped `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `MS_AUTHORITY_HOST` from all six
Microsoft manifests while `MicrosoftCredentials.from_env` went on reading them.
`TestNothingIsReadThatIsNotDeclared` now covers all 27 by comparing declared
fields against `os.environ` reads in each package, with an allowlist for the
three that genuinely are not credentials — and a second test that fails when
that allowlist outlives its variables. It found two more on its first run:
`QUICKBOOKS_ACCESS_TOKEN` and `STRIPE_ACCOUNT`, both undeclared before this.

**4 — `ConnectionInspector`.** (D4, part 2)
`connectors/inspect.py`. Surface it on `RuntimeFacade.connections()`,
`loom connections`, `loom doctor`, and the MCP `list_connections` tool.
*Test:* a fake store × a fake environ × the 27 real manifests, asserting all
six states; and that it opens no socket and imports no `tools` module.

*Status: done.* Three things the work changed or found.

**`AuthSpec` needed a fifth field, and the smoke run is what showed it.**
Several services accept *alternative* credential modes: Google takes a
ready-made access token, **or** a client-id/secret/refresh trio, **or** a
service account file; Microsoft takes `MS_*`, **or** the `AZURE_*` names the
Azure SDKs already put in an environment, **or** a ready-made Graph token;
ClickUp and GitLab each take either of two tokens. A flat `required` cannot say
that, and both readings are wrong: requiring every field reports a working
Google deployment as missing five variables, and requiring any reports an empty
one as configured. `AuthField.mode` groups fields into one way of
authenticating, `AuthSpec.satisfied_by()` owns the rule, and the values mirror
`GoogleCredentials.mode` / `MicrosoftCredentials.mode` — which already existed
in the clients and were the ground truth all along.

**The shortfall names the nearest mode, and the tie-break is the half that
matters.** Fewest-missing, then *most already present*. Two thirds of the way
through Google's trio every mode is one variable short, so counting alone picks
whichever was declared first and answers "set `GOOGLE_ACCESS_TOKEN`" to
somebody plainly in the middle of the other one. The first version did exactly
that, and its own test caught it.

**`loom doctor` can now answer the question it was asked.** It reported
*"toolsets: 27 reachable"* and *"credentials: 3 stored"* on adjacent lines and
could not say whether Jira was usable. It now adds
*"connections: 6 of 27 configured: gmail, google_calendar, …"*. Never a
failure: an unconfigured integration is the normal state for a project that
does not use it, and exiting 1 on one would make `loom doctor` red in almost
every repository — which is how a checker stops being read.

**5 — Extract `ConnectFlow`; `cli/auth_commands` becomes an adapter.**
`connectors/flows.py`. `OAuthBrowserFlow`/`OAuthDeviceFlow` are lifts;
`ApiKeyFlow` and `AppRegistrationStore` are new. `facade.connect/disconnect`;
`RemoteFacade` refuses; `AuthorizedFacade` gates on `credentials:connect`.
*Test:* `tests/test_cli_auth.py` keeps passing (that is the regression bar for a
pure extraction); `test_surface_parity` covers the three new port methods;
`tests/test_connect_flows.py` drives the browser path with no browser; and
**`tests/test_layering.py`** lands in this step rather than later, because this
is the step that could breach the boundary (§7).

*Status: done.* Five things worth recording.

**The layering test found a pre-existing breach on its first run**, and it was
not a breach. `agents/interaction.py` imports `rich` — inside a function, inside
`try: … except ImportError:`, with a stdlib fallback. That is an *optional
upgrade*, not a dependency: `CLIUserInteraction` works on a bare
`pip install loomsdk`, which is the whole claim. So the rule is stated as a
rule rather than patched with an exemption — `_hard_imports()` subtracts
anything guarded by `ImportError`, and a test asserts the guarded shape is not
reported, or the rule collapses into the list it was written to avoid.

**Two seams were only patchable by accident, and one would have opened a real
browser.** `OAuthBrowserFlow.open_browser` defaulted to `webbrowser.open`, and a
dataclass field default is evaluated at **import** — so it froze whatever
`webbrowser.open` was then, and a test patching it afterwards would have had no
effect on somebody's actual machine. `open_in_browser()` looks the attribute up
when called. Two lines in `tests/test_cli_auth.py` changed with it, both of
which asserted *where the code lives* (`auth_commands.webbrowser`,
`auth_commands.sys`) rather than what it does; both reached the shared module
object anyway, so they now say that directly.

**`loom connect jira` works.** It looked the *credential* name up in the OAuth
provider registry and refused with *"'jira' is not a known provider"*.
`need_for()` reads the manifests instead, and the refusal now says *"'jira' is
served by the 'atlassian' provider"*. Scopes come from the toolsets that read
the credential rather than the provider's defaults — narrower, and the union
across the five Google toolsets sharing one token, for the reason `GoogleAuth`
merges them.

**The conformance kit failed a correct flow, and that is the bug it exists to
prevent.** `verify_connect_flow` checked whether the token appears in the
outcome's `repr` — and a one-character test token is inside the word
"connected". A kit that fails correct code is one people switch off, so it now
requires a secret of eight characters or more before believing a substring
match, with the false positive pinned as its own test.

**An api-key `loom connect` was written and then deleted.** It would have been
dead: every store-backed credential the shipped toolsets declare is `oauth2`,
and the twelve api-key toolsets read environment variables and no
`CredentialStore` at all — so connecting one would store a value nothing looks
up. `ApiKeyFlow` stays, `facade.connect` routes to it, and it is exercised by a
third-party spec in the tests. What is missing is a store path in those twelve
clients, which is a change to them and is the natural next step after this one.

**6 — Bind the credential store during authoring reads.** (D5)
Thread `credentials` from `LocalFacade` → `WorkflowCodingAgent` →
`build_coding_tools` → `_call_read_operation`, wrapped in
`credential_store_scope`.
*Test:* `tests/test_ask_user_wiring.py`'s discipline — a **wiring** test, not a
unit test, because this exact class of defect (a complete, fully unit-tested
module that no caller passes in) is what `interaction.py` shipped with. Assert
from `LocalFacade.author` that a store bound at the CLI reaches a toolset
function called during authoring.

**7 — `not_connected` outcome + `connect_toolset` + `ConnectGate`.**
`agents/coding_tools.py`, `agents/limits.py`.
*Test:* the tool is **absent** from `build_coding_tools` with no interaction or
no flow (the `observe_target` rule); present with both; `--no-ask` omits it;
the gate refuses a third attempt; `_call_read_operation` returns the structured
payload for `CredentialNotFound` and the *unchanged* generic note for a
`TimeoutError`, so widening the catch cannot swallow a real failure.

**8 — `ConnectionStage`, prompt rules, docs.**
`agents/stages.py`; `DEFAULT_SYSTEM_PROMPT` gains the two roster rules;
`docs/guides/connections.md` (new, every snippet executed by
`scripts/docs_examples.py`); `docs/guides/cli.md` gains `loom connections`;
`docs/guides/toolsets.md` gains the `AuthSpec` section;
`docs/seams/` regenerated for `ConnectFlow` and `SecretPrompt`;
`CLAUDE.md` and `CHANGELOG.md`.
*Test:* both directions on the stage; `tests/test_cli_docs.py` covers the new
subcommand and flags.

**9 — Paper cuts.** (D6) `find_operation` accepts a function name;
`_resolve_target` names the mapped provider.

### Steps 6–9: done, and what they cost

*Status: done*, in `tests/test_authoring_connections.py`. Four things the work
found, three of them defects in this plan's own earlier steps.

**The app registration was built and never wired**, which is this phase's own
defect twice over. `AppRegistrationStore` shipped in step 5 and
`LocalFacade.connect` never called it, so `connect_toolset("jira")` reached
`OAuthBrowserFlow` with no client id, the flow correctly refused, and the
caller saw a connect that **returned in a tenth of a second having printed
nothing**. Capability present, nobody wired to it — the same shape as D1, and
as the trigger and `@pure` gaps. `connect` now reads the remembered app,
asks for one through `SecretPrompt` when there is none, and remembers it per
*provider* so one Atlassian app serves `jira` and `confluence`. `on_connect_event`
is the other half: without a renderer the flow's `needs_app` and
`redirect_ready` events went nowhere, and a connection that stops at "register
an app first" is indistinguishable from one that did nothing.

**A wrapper made every workflow unclassified.** The prompt tells the agent to
put I/O inside a step, so it writes `async def resolve_project` around
`jira_resolve_project` — and the *durable call* names the wrapper. Reading only
the call site meant a two-read workflow reported `unclassified resolve_project`
and asked for confirmation, which is an ask nobody can act on and so an ask
people learn to answer without reading. `impact_of` now sees one level through,
and only when every toolset call inside is declared and they **agree**: a
wrapper doing a read and a write is the write, and one calling something
undeclared stays unclassified. Builtins are excluded, or every wrapper would
stay unclassified and the whole thing would be inert.

**A private member on the Protocol broke every structural check.** The
`_app_registration` helper landed inside the `RuntimeFacade` Protocol body
rather than on `LocalFacade` — an anchored replace matching `disconnect`, which
appears in both. `runtime_checkable` verifies every declared member, private
ones included, so `isinstance(LocalFacade(...), RuntimeFacade)` answered False
and `build_workflow_tools` responded by wrapping the facade in a second
`LocalFacade`. `self.runtime` became a facade, `.workflows` became a bound
method, and it surfaced three layers away as *"'function' object has no
attribute 'values'"*. `test_the_port_declares_no_private_members` is the guard.

**The prompt rule was written and then deleted**, and that is the search
working rather than a reversal. "A missing credential is not a reason to change
anything" put the prompt 355 over budget; merging it into "Only the toolsets
listed above exist" recovered 74; and then the real finding was that the
`not_connected` payload **already says it**, in context, on the one turn where
it matters — where the prompt version is paid for on every turn of every job.
Removed, exactly as `node_contract`'s worked example was. The prompt ended at
11 895 against a ceiling of 11 900.

---

## 7. Test plan

Beyond the per-step tests above, four suites that exist because of how this
subsystem fails rather than because of what it does.

**`tests/test_toolset_discovery.py` — the scope boundary.** The single
invariant this phase must not break, stated three ways: a bare `Runtime`'s
`ctx.agent("summarise")` resolves **zero** tools; `ctx.agent(toolsets=["jira"])`
resolves Jira; the coding agent's registry *sees* Jira. If a future change
collapses the two scopes, this is what fails, and it fails on the first one.

**`tests/test_connection_wiring.py` — the seams, not the units.**
`interaction.py` shipped complete, fully unit-tested, and wired by nobody. So:
does `loom author` reach the built-in catalogue? Does the CLI's credential store
reach an authoring-time read? Is `connect_toolset` offered on the CLI and not
in `--json`/`--no-ask`? A module's own tests cannot answer any of these,
because they construct the thing themselves.

**`loom.testing.conformance.verify_connect_flow` — a kit, not an adapter.**
The position `verify_event_source`, `verify_probe` and `verify_browser_session`
already take. It asserts what an author is least likely to test: an outcome
that never carries a token or a client secret; a `state` mismatch refused; a
PKCE verifier that is fresh per attempt; nothing written to the store on a
failed exchange; and an interrupted flow leaving no half-credential.

**`tests/test_layering.py` — the boundary, asserted rather than remembered.**
Greps `src/loom` outside `cli/` for an import of `loom.cli` or of a `[cli]`
extra (`rich`, `prompt_toolkit`, `textual`) or of `argparse`, allowing only
modules named `__main__.py`. It passes on the tree as it stands today — one
edge, from `mcp_server/__main__.py`, and one `argparse`, in
`toolsets/google/setup.py` — so it is a ratchet, not a cleanup. A second test
asserts that importing `loom.connectors.flows` opens no browser and reads no
stdin: `webbrowser.open` and `getpass.getpass` are patched to raise, and the
module is imported and its adapters constructed.

This is the shape `tests/test_cli_session.py:110` already uses to keep
`repl/commands.py` off the facade, and `test_host_integration.py` to keep a
host off `runtime._…` — both because the property is one that creeps back one
import at a time, and neither module's own tests can see it.

**`tests/test_redaction.py` extension.** `ConnectOutcome`, `ConnectionStatus`
and the `not_connected` payload are asserted to be free of token material —
and `oauth-app:*` is added to the redaction denylist under the whole-word rule,
beside `storage_state`, for the same reason: a client secret in a trace is a
credential in durable storage.

---

## 8. Alternatives considered, and why not

**Call `register_available_toolsets()` from `targets.resolve()`.** One line,
fixes D1, and reintroduces exactly the hazard `toolsets/registry.py:138-152`
documents: every `loom run` in the project would hand an unscoped
`ctx.agent("summarise this")` all 27 toolsets, `jira_delete_issue` included.
The two-scope split costs more code and keeps that closed.

**Seed only when authoring.** Better, but it makes discovery a property of
*which command you typed* rather than of the registry. `loom edit` and the
session would each need to remember, and a host embedding `LocalFacade`
directly would still see nothing. The registry is the right place because the
question — "what may be named?" — is the registry's.

**Infer the OAuth provider from the toolset id.** `slack`, `github`, `hubspot`
and `zoom` match; `jira`, `gmail`, `google_drive`, `teams`, `onedrive`,
`sharepoint`, `onenote`, `outlook_mail` and `outlook_calendar` do not. A rule
right for four of thirteen is the guess `DEFAULT_SYSTEM_PROMPT` names as the
tell, and `from_steps`'s name-guessing already under-classifies 14 % of this
repository's own operations.

**Collect the client secret through `ask_user`.** It is the shortest path and
it writes the secret into `--save-answers`, which is a build-input file people
commit. `SecretPrompt` exists for that reason alone.

**Make `not_connected` an error so the repair loop fixes it.** The repair loop
cannot fix a missing credential, and the cheapest change it *can* make is to
delete the integration — producing a workflow that passes every stage having
removed the thing that was asked for. This repository has shipped that failure
three times; §3.5 is the standing answer.

**Lift `_run_pkce_flow` verbatim, `Printer` and all.** It is the smaller diff
and it makes `loom/connectors/` depend on `loom/cli/output.py` — the second
library→CLI edge in the codebase, in the layer furthest from the CLI. The
`ConnectEvent` seam costs one dataclass and keeps rendering in the tier that
owns `rich`.

**Let `ConnectFlow` default to the console adapters.** Then importing
`loom.connectors.flows` means a library that can open a browser and read stdin
because it was imported — the ambient behaviour `LocalFacade.user_interaction`
was explicitly placed to avoid. Composition at `targets.resolve()` costs two
lines and makes the CLI the only thing that opts in.

**Park the run / suspend the authoring job during OAuth.** Authoring is a
snapshot, not a durable workflow (`agents/session_store.py`) — there is no
journal to serve and nothing has been made deterministic. A browser flow that
completes in ninety seconds does not need one, and `--resume` already covers
the interruption.

---

## 9. File change inventory

**New**

```
src/loom/connectors/inspect.py            ConnectionInspector, ConnectionStatus
src/loom/connectors/flows.py              ConnectFlow/SecretPrompt protocols,
                                          ConnectEvent, ConnectRequest/Outcome,
                                          OAuthBrowserFlow, OAuthDeviceFlow,
                                          ApiKeyFlow, ConsoleSecretPrompt,
                                          AppRegistrationStore
                                          (stdlib + httpx; no [cli] extra)
src/loom/testing/conformance/connect.py   verify_connect_flow
docs/guides/connections.md
phases/phase-15-toolset-discovery-and-connections.md   (this file)
tests/test_toolset_discovery.py
tests/test_connection_wiring.py
tests/test_manifest_auth.py
tests/test_connect_flows.py
tests/test_layering.py                    library must not import loom.cli
                                          or a [cli] extra; importing the
                                          flows opens no browser, reads no stdin
```

**Modified**

```
src/loom/toolsets/catalog.py        merge helpers; describe(detail="roster");
                                    empty-catalogue sentence
src/loom/toolsets/registry.py       BuiltinToolsetCatalog, builtin_catalog()
src/loom/toolsets/manifest.py       AuthSpec/AuthField; find_operation by function
src/loom/toolsets/*/manifest.py     27 migrations (the only bulk edit)
src/loom/agents/tool_registry.py    tier lists replace 7 overrides; catalogue_ids()
src/loom/agents/coding_agent.py     catalogue_ids(); roster; credentials threaded
src/loom/agents/coding_tools.py     not_connected payload; connect_toolset;
                                    credential_store_scope around the invocation
src/loom/agents/stages.py           ConnectionStage
src/loom/agents/limits.py           ConnectGate
src/loom/facade.py                  connections/connect/disconnect on the port;
                                    LocalFacade gains connect_flow= and
                                    secret_prompt= seams (default None, as
                                    user_interaction does); RemoteFacade refuses
src/loom/identity/facade.py         credentials:connect
src/loom/cli/auth_commands.py       adapter over ConnectFlow; renders
                                    ConnectEvent as Printer lines
src/loom/cli/targets.py             composes the flow + secret prompt into
                                    LocalFacade, beside _interaction()
src/loom/cli/commands.py            loom connections
src/loom/cli/doctor.py              per-toolset connection status
src/loom/mcp_server/tools.py        list_connections, connect_toolset (gated)
src/loom/core/redaction.py          oauth-app:* on the denylist
docs/guides/{cli,toolsets,coding-agent}.md
CLAUDE.md, CHANGELOG.md
```

No new runtime dependency, and nothing moves tiers. `keyring`/`cryptography`
stay the existing `[credentials]` extra; `rich`/`prompt_toolkit` stay confined
to `src/loom/cli/`; `MemoryCredentialStore`, `ApiKeyFlow` and `OAuthBrowserFlow`
work on the core `pip install loomsdk`. The `loom` and `loomsdk` console scripts
remain one wheel — §3.6 is a layering property enforced by a test, not by
packaging.

---

## Sources

- [RFC 8252 — OAuth 2.0 for Native Apps](https://www.rfc-editor.org/rfc/rfc8252.html) — loopback redirect, mandatory PKCE, a native client's secret is not confidential.
- [n8n — credential configuration and OAuth callback URL](https://docs.n8n.io/integrations/builtin/credentials/httprequest) — the read-only redirect-URI field, client id/secret, encrypted at rest, auto-refresh.
- [n8n — Google OAuth2 generic credential](https://docs.n8n.io/integrations/builtin/credentials/google/oauth-generic) — one credential type per integration, scopes declared by the integration.
- [Arcade — Authorized tool calling](https://docs.arcade.dev/en/home/auth/auth-tool-calling) — a tool call that needs an account returns an authorization request rather than raising.
- [MCP — Authorization (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) — protected-resource metadata, DCR, and the elicitation channel a server asks over.
