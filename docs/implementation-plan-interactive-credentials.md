# Implementation Plan: Interactive Agent, Programmatic Credentials, Runtime Env Vars, Pre-configured OAuth

<!-- docs-illustrative -->

## Executive Summary

Four interconnected features that strengthen LOOM's credential management and agent interactivity:

1. **User Ask Tool** — An optional, pluggable tool for `WorkflowCodingAgent` and workflow agents to interactively ask the user clarifying questions mid-loop
2. **Programmatic Token Passing** — Pass authentication tokens to toolset nodes at execution time without environment variables
3. **Runtime Environment Variables** — Inject environment variables into a workflow at the time of execution
4. **Pre-configured OAuth Providers** — A registry of well-known OAuth endpoints so users only supply `client_id` and `client_secret`

---

## Table of Contents

- [1. Existing Architecture Summary](#1-existing-architecture-summary)
- [2. Feature 1: User Ask Tool](#2-feature-1-user-ask-tool)
- [3. Feature 2: Programmatic Token Passing](#3-feature-2-programmatic-token-passing)
- [4. Feature 3: Runtime Environment Variables](#4-feature-3-runtime-environment-variables)
- [5. Feature 4: Pre-configured OAuth Providers](#5-feature-4-pre-configured-oauth-providers)
- [6. Implementation Phases](#6-implementation-phases)
- [7. Test Plan](#7-test-plan)
- [8. Multi-Angle Review](#8-multi-angle-review)
- [9. File Change Inventory](#9-file-change-inventory)

---

## 1. Existing Architecture Summary

### Relevant Existing Components

| Component | File | Role |
|---|---|---|
| `WorkflowCodingAgent` | `agents/coding_agent.py` | ReAct agent that generates workflow code. Uses `build_coding_tools()` for discovery/validation |
| `build_coding_tools()` | `agents/coding_tools.py` | Returns 6 tools: `search_toolsets`, `show_toolset`, `get_tool_contract`, `get_tool_docs`, `call_read_operation`, `validate_code` |
| `Agent` | `agents/agent.py` | Generic agent definition with tools, executor, and session support |
| `AgentExecutor` | `agents/executor.py` | Protocol for agent turn loops. `AgentContext` threads ambient state |
| `BuiltInAgentRuntime` | `agents/runner.py` | LOOM's own ReAct executor |
| `Tool` | `agents/tools.py` | Dataclass: `fn`, `name`, `description`, `parameters`, `needs_approval` |
| `ConnectionBroker` | `toolsets/connections.py` | Resolves connection IDs to `Credential`s from config dict or env vars |
| `CredentialStore` | `connectors/credentials.py` | Protocol: `get(name) -> Secret`, `put(name, StoredCredential)`, `forget`, `names`. Three impls: Memory, EncryptedFile, Keyring |
| `OAuthClient` | `connectors/oauth_client.py` | PKCE + device-code flows. Implements `Refresher`. Takes explicit `authorization_endpoint`, `token_endpoint`, `client_id`, etc. |
| `MetadataRefresher` | `connectors/oauth_client.py` | Reads OAuth config from credential metadata for auto-refresh |
| `GoogleAuth` | `toolsets/google/auth.py` | Google-specific OAuth with 3 credential modes. Uses `resolve_bearer_token()` for store integration |
| `credential_store_scope()` | `connectors/credentials.py` | ContextVar binding a `CredentialStore` for the duration of a step attempt |
| `Runtime` | `runtime/engine.py` | Accepts `credentials` (CredentialStore), `deps`, `toolsets`, `broker`, etc. |
| `Runtime.run()` | `runtime/engine.py` | Takes `target`, `input`, `deps`, `metadata`. No env vars or credentials param today |
| `Context` | `runtime/context.py` | Workflow API surface. `ctx.step()`, `ctx.agent()`, `ctx.sleep()`, etc. |
| `loom connect` | `cli/auth_commands.py` | OAuth flows storing credentials. Requires all endpoints to be specified via flags/env vars |

### Key Architectural Constraints

- **Determinism**: Workflow bodies must be deterministic. Credential resolution happens inside `@step` bodies, bound via `credential_store_scope` ContextVar
- **No secrets in journals**: `Secret[str]` type prevents accidental serialization — `StoredCredential.token` is `Secret`, not `str`
- **Lazy resolution**: Toolsets resolve credentials at invocation time, not at registration time
- **Loose coupling**: Each toolset handles its own auth (Google reads env vars + CredentialStore fallback; Jira reads JIRA_* env vars + store). No central auth dispatch yet
- **ConnectionBroker is unused by toolsets**: Despite being implemented and tested, no toolset client (Jira, Confluence, Google) calls `ConnectionBroker`. The actual credential path is `CredentialStore` + env vars via `resolve_bearer_token(credential_name)`. This means the programmatic token passing feature should extend `CredentialStore`, not `ConnectionBroker`
- **No `agents/__init__.py`**: The agents package has no `__init__.py`; imports are by submodule path (e.g., `workflow_builder.agents.coding_agent`). New agent-layer types should follow this pattern

---

## 2. Feature 1: User Ask Tool

### 2.1 Problem Statement

The `WorkflowCodingAgent` and workflow-running agents (`ctx.agent()`) currently have no mechanism to ask the user a clarifying question mid-loop. When the agent encounters ambiguity — "which Jira project did you mean?", "should this step retry on failure?" — it must guess or fail. A user ask tool lets the agent **pause, ask, and resume** with the answer.

### 2.2 High-Level Design

```
┌─────────────────────────────────────┐
│        Agent Turn Loop              │
│  (BuiltInAgentRuntime / any exec.)  │
│                                     │
│  Model calls `ask_user` tool        │
│         │                           │
│         ▼                           │
│  ┌─────────────────┐                │
│  │ UserInteraction  │ ◄─ Protocol   │
│  │   Protocol       │               │
│  └────────┬────────┘                │
│           │                         │
└───────────┼─────────────────────────┘
            │
    ┌───────┴────────┐
    │  Implementations│
    ├────────────────┤
    │ CLIUserInput   │  ← Default: rich prompt / input()
    │ CallbackInput  │  ← Any callable(question) -> str
    │ SuspendInput   │  ← For durable workflows: raises Suspend
    │ WebSocketInput │  ← Future: MCP/WebSocket
    └────────────────┘
```

### 2.3 Interfaces

```python
# connectors/interaction.py (NEW)

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable
from pydantic import BaseModel, Field


class UserQuestion(BaseModel):
    """What the agent wants to know."""
    question: str
    options: list[str] | None = None
    input_type: str = "text"  # "text" | "select" | "confirm" | "multiselect"
    context: str = ""
    allow_skip: bool = False
    default: str | None = None


class UserResponse(BaseModel):
    """What the user answered."""
    answer: str
    skipped: bool = False


@runtime_checkable
class UserInteraction(Protocol):
    """How the agent asks a human a question and gets an answer.

    Implementations are *transport-specific*: CLI reads from stdin,
    MCP sends a suspend event, a web UI pushes over a WebSocket.
    The agent turn loop sees only this protocol.
    """
    async def ask(self, question: UserQuestion) -> UserResponse: ...


class CLIUserInteraction:
    """Default: reads from stdin. Falls back to input() when rich is absent."""

    async def ask(self, question: UserQuestion) -> UserResponse:
        ...  # Implementation uses rich.prompt or builtins.input


class CallbackUserInteraction:
    """Wraps any async callable(UserQuestion) -> UserResponse."""

    def __init__(self, callback: Callable[[UserQuestion], Awaitable[UserResponse]]) -> None:
        self._callback = callback

    async def ask(self, question: UserQuestion) -> UserResponse:
        return await self._callback(question)


class SuspendUserInteraction:
    """For durable workflows: raises Suspend so the run parks until the
    answer arrives as an event. Used by ctx.agent() inside a workflow."""

    async def ask(self, question: UserQuestion) -> UserResponse:
        raise Suspend(
            f"Agent is asking: {question.question}",
            awaiting_event=f"user_response:{question.question[:50]}",
        )
```

### 2.4 Tool Definition

```python
# agents/coding_tools.py — added to build_coding_tools()

@tool
async def ask_user(
    question: str,
    options: list[str] | None = None,
    input_type: str = "text",
    context: str = "",
) -> str:
    """Ask the user a clarifying question when the spec is ambiguous.

    Use ONLY when blocked — when you cannot determine from the spec alone
    what the workflow should do. Do not ask to confirm things the spec
    already states. Maximum 5 questions per generation.

    Args:
        question: The question to ask. Keep it short and direct.
        options: Optional predefined choices for select/multiselect.
        input_type: "text", "select", "confirm", or "multiselect".
        context: Why you are asking (optional).
    """
    ...
```

### 2.5 Integration Points

**WorkflowCodingAgent:**
```python
class WorkflowCodingAgent:
    def __init__(
        self,
        model: object,
        *,
        user_interaction: UserInteraction | None = None,  # NEW
        ...
    ) -> None:
        self._user_interaction = user_interaction
        ...
```

When `user_interaction` is provided, `build_coding_tools()` includes the `ask_user` tool wired to that interaction handler. When `None`, the tool is omitted — the agent cannot ask.

**Agent (general):**
```python
@dataclass
class Agent(Generic[OutputT]):
    ...
    user_interaction: UserInteraction | None = None  # NEW
```

When the agent has a `user_interaction`, an `ask_user` tool is injected into its tool list automatically by `_resolve_executor()`.

**CLI wiring:**
```python
# In loom CLI, when WorkflowCodingAgent is constructed:
agent = WorkflowCodingAgent(
    model=provider,
    user_interaction=CLIUserInteraction(),  # Default for CLI
)
```

### 2.6 Guardrails

- **Turn budget for questions**: `max_questions: int = 5` on the tool, enforced by a counter in the closure. After the limit, the tool returns an error telling the model to proceed with available information
- **No questions in smoke/repair**: The ask_user tool is disabled during repair rounds and smoke tests (the model sees a "not available in this phase" error)
- **Structured responses**: When `options` are provided, the response is validated against them

### 2.7 Data Flow

```
User writes spec → WorkflowCodingAgent.generate(spec)
  → Agent turn loop starts
    → Model calls search_toolsets, show_toolset (discovery)
    → Model encounters ambiguity: "which project?"
    → Model calls ask_user(question="Which Jira project...", options=["PROJ-A", "PROJ-B"])
    → ask_user.fn delegates to UserInteraction.ask()
      → CLIUserInteraction prints prompt, reads stdin → "PROJ-A"
    → Tool returns "PROJ-A" to model
    → Model uses answer in call_read_operation and code generation
    → Model calls validate_code, final_output
  → CodingResult returned
```

---

## 3. Feature 2: Programmatic Token Passing

### 3.1 Problem Statement

Currently, toolset credentials must be configured via environment variables (`GOOGLE_ACCESS_TOKEN`, `JIRA_API_TOKEN`, `LOOM_CONN_*`) or through `loom connect`. There is no way to pass a token programmatically at `Runtime.run()` time, which is needed when:

- An orchestrator has short-lived tokens it mints per-run
- A web API receives a user's OAuth token and needs to pass it to a workflow
- A test wants to inject a specific token without touching env vars

### 3.2 High-Level Design

```
┌──────────────────────────────────────────────┐
│                  Caller                       │
│                                              │
│  result = await rt.run(                      │
│      my_workflow,                            │
│      input_data,                             │
│      credentials={"google": token,           │
│                   "jira": jira_token},       │
│  )                                           │
└──────────┬───────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────┐
│           Runtime._drive_inner()             │
│                                              │
│  Builds a RunCredentialStore that layers:     │
│  1. Per-run credentials (from caller)        │
│  2. Runtime.credentials (CredentialStore)     │
│  3. Environment variable fallback            │
│                                              │
│  Binds via credential_store_scope()          │
└──────────┬───────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────┐
│           Step body executes                  │
│                                              │
│  Google toolset calls GoogleAuth.token()     │
│  → resolve_bearer_token("google")            │
│  → current_credential_store().get("google")  │
│  → RunCredentialStore checks per-run first   │
│  → Returns the token caller passed           │
└──────────────────────────────────────────────┘
```

### 3.3 Interfaces

```python
# connectors/credentials.py — NEW class

class LayeredCredentialStore(BaseCredentialStore):
    """A credential store that checks multiple sources in priority order.

    Used internally to layer per-run credentials on top of the
    Runtime's configured CredentialStore.
    """

    def __init__(
        self,
        *layers: CredentialStore,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self._layers = layers

    async def _read(self, name: str) -> StoredCredential | None:
        for layer in self._layers:
            try:
                # peek if available, otherwise try get path
                if hasattr(layer, 'peek'):
                    result = await layer.peek(name)
                    if result is not None:
                        return result
                else:
                    secret = await layer.get(name)
                    return StoredCredential(token=Secret(secret.reveal()))
            except CredentialNotFound:
                continue
        return None

    async def _write(self, name: str, credential: StoredCredential) -> None:
        # Writes go to the first writable layer
        await self._layers[0].put(name, credential)

    async def _delete(self, name: str) -> None:
        await self._layers[0].forget(name)

    async def _list(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for layer in self._layers:
            for name in await layer.names():
                if name not in seen:
                    seen.add(name)
                    result.append(name)
        return sorted(result)
```

### 3.4 Runtime.run() Extension

```python
# runtime/engine.py

class Runtime:
    async def run(
        self,
        target: WorkflowDefinition[Any, Any, Any] | str,
        input: Any = None,
        *,
        deps: Any = None,
        credentials: dict[str, str] | CredentialStore | None = None,  # NEW
        ...
    ) -> ExecutionResult:
```

When `credentials` is a `dict[str, str]`, it is converted to a `MemoryCredentialStore` populated with `StoredCredential` objects. If the Runtime also has a `self.credentials`, they are layered:

```python
run_store = _build_run_credentials(credentials, self.credentials)
```

The `credential_store_scope(run_store)` binding in `_attempt_loop` (context.py line 279) already handles making this available to toolset code.

### 3.5 ConnectionBroker Extension

```python
# toolsets/connections.py

class ConnectionBroker:
    def __init__(
        self,
        *,
        config: dict[str, dict[str, Any]] | None = None,
        tokens: dict[str, str] | None = None,  # NEW: simple name→token map
        clock: Clock | None = None,
    ) -> None:
        self._config = config or {}
        self._tokens = tokens or {}
        ...

    async def resolve(self, connection_id: str, ...) -> Credential:
        # 1. Check simple tokens first (new)
        if connection_id in self._tokens:
            return Credential(token=self._tokens[connection_id], ...)
        # 2. Check explicit config (existing)
        if connection_id in self._config:
            ...
        # 3. Fall back to env vars (existing)
        ...
```

### 3.6 Data Flow

```
Caller passes credentials={"google": "ya29.xxx", "jira": "api_token_yyy"}
  → Runtime.run() converts to MemoryCredentialStore
  → Layers with Runtime.credentials (if any)
  → Passes to _drive_inner as part of run setup
  → _drive_inner builds Context with layered store
  → credential_store_scope(layered_store) bound in _attempt_loop
  → Step executes: Google toolset calls GoogleAuth.token()
    → resolve_bearer_token("google") → LayeredCredentialStore.get("google")
    → Returns Secret("ya29.xxx") → GoogleAuth returns it
  → Token never touches journal, never in env
```

---

## 4. Feature 3: Runtime Environment Variables

### 4.1 Problem Statement

Workflows that call external services often depend on configuration that varies between environments (dev/staging/prod) or between runs. Today this requires setting process-level environment variables before starting the Runtime, which is:
- Not isolated between concurrent runs
- Not portable between test and production
- Not overridable per-run

### 4.2 High-Level Design

```
┌────────────────────────────────────────────────────┐
│                     Caller                          │
│                                                    │
│  result = await rt.run(                            │
│      my_workflow,                                  │
│      input_data,                                   │
│      env={"API_BASE_URL": "https://api.prod.com", │
│           "LOG_LEVEL": "DEBUG"},                   │
│  )                                                 │
└──────────┬─────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────┐
│          Runtime._drive_inner()                     │
│                                                    │
│  Sets RunEnvironment on the Context                │
│  (ContextVar, not os.environ)                      │
└──────────┬─────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────┐
│          Context exposes ctx.env                    │
│                                                    │
│  @step body:                                       │
│    url = ctx.env.get("API_BASE_URL", default)      │
│    # OR: toolset reads from RunEnvironment         │
│                                                    │
│  Precedence:                                       │
│  1. Per-run env (from caller)                      │
│  2. Runtime-level env (from Runtime constructor)   │
│  3. os.environ (existing behavior)                 │
└────────────────────────────────────────────────────┘
```

### 4.3 Interfaces

```python
# runtime/environment.py (NEW)

from __future__ import annotations
import os
from collections.abc import Iterator, Mapping
from contextvars import ContextVar


class RunEnvironment(Mapping[str, str]):
    """A layered, read-only environment for one workflow run.

    Checks per-run overrides first, then Runtime-level defaults,
    then os.environ. Never mutates os.environ — concurrent runs
    on the same event loop each see their own overrides.
    """

    def __init__(
        self,
        run_env: dict[str, str] | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> None:
        self._run = run_env or {}
        self._runtime = runtime_env or {}

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in self._run:
            return self._run[key]
        if key in self._runtime:
            return self._runtime[key]
        return os.environ.get(key, default)

    def __getitem__(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key in self._run or key in self._runtime or key in os.environ

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for key in self._run:
            seen.add(key)
            yield key
        for key in self._runtime:
            if key not in seen:
                seen.add(key)
                yield key

    def __len__(self) -> int:
        return len(set(self._run) | set(self._runtime))

    def to_dict(self) -> dict[str, str]:
        """The overrides only — never the full process environment."""
        merged = dict(self._runtime)
        merged.update(self._run)
        return merged


# ContextVar for per-run environment
_current_env: ContextVar[RunEnvironment | None] = ContextVar(
    "loom_run_environment", default=None
)


def current_run_environment() -> RunEnvironment | None:
    return _current_env.get()
```

### 4.4 Integration with Runtime and Context

```python
# runtime/engine.py — Runtime changes

class Runtime:
    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,  # NEW: runtime-level defaults
        ...
    ) -> None:
        self.env = env or {}
        ...

    async def run(
        self,
        target: ...,
        input: Any = None,
        *,
        env: dict[str, str] | None = None,  # NEW: per-run overrides
        ...
    ) -> ExecutionResult:
        ...
        # Store env in record.metadata for replay awareness
        if env:
            record.metadata["run_env"] = env
        ...
```

```python
# runtime/context.py — Context changes

class Context:
    def __init__(self, ...) -> None:
        ...
        self.env: RunEnvironment  # NEW — available to workflow bodies
```

### 4.5 Toolset Integration

Toolsets that currently read from `os.environ` get a migration path:

```python
# toolsets/google/auth.py

@classmethod
def from_env(cls, env: dict[str, str] | None = None) -> GoogleCredentials:
    """Reads from the provided env dict, falling back to os.environ.

    When called inside a run, the RunEnvironment provides per-run
    overrides transparently via current_run_environment().
    """
    if env is None:
        run_env = current_run_environment()
        source = run_env if run_env is not None else os.environ
    else:
        source = env
    ...
```

### 4.6 CLI Extension

```bash
loom run my_workflow --input '{}' --env API_KEY=xxx --env BASE_URL=https://...
# Or:
loom run my_workflow --input '{}' --env-file .env.prod
```

### 4.7 Data Flow

```
Caller passes env={"API_BASE_URL": "https://prod.api.com"}
  → Runtime.run() stores in record.metadata["run_env"]
  → _drive_inner creates RunEnvironment(run_env=env, runtime_env=rt.env)
  → Context.env = run_environment
  → ContextVar _current_env bound for this run
  → Step body: ctx.env.get("API_BASE_URL") → "https://prod.api.com"
  → Toolset: GoogleCredentials.from_env() checks current_run_environment()
  → After run: env overrides NOT journaled (not determinism-affecting)
```

---

## 5. Feature 4: Pre-configured OAuth Providers

### 5.1 Problem Statement

`loom connect <name>` requires the user to specify `--token-endpoint`, `--authorization-endpoint`, `--client-id`, and `--client-secret` as flags or env vars. For well-known providers (Google, Jira/Atlassian, GitHub, Slack, Microsoft, etc.), the authorization and token URLs are public knowledge. Users should only need to supply `client_id` and `client_secret`.

### 5.2 High-Level Design

```
┌─────────────────────────────────────────────────┐
│           OAuth Provider Registry                │
│                                                 │
│  PROVIDERS = {                                  │
│    "google": OAuthProviderConfig(               │
│      authorization_endpoint="https://...",      │
│      token_endpoint="https://...",              │
│      device_authorization_endpoint=None,        │
│      default_scopes=("openid", "email", ...),   │
│      supports_pkce=True,                        │
│    ),                                           │
│    "atlassian": OAuthProviderConfig(...),        │
│    "github": OAuthProviderConfig(...),           │
│    "slack": OAuthProviderConfig(...),            │
│    "microsoft": OAuthProviderConfig(...),        │
│  }                                              │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│           loom connect google                    │
│           --client-id=xxx                        │
│           --client-secret=yyy                    │
│                                                 │
│  Resolves: provider "google" →                  │
│    authorization_endpoint already known          │
│    token_endpoint already known                  │
│    scopes from provider + any --scope flags      │
│    Only client_id and client_secret needed       │
└─────────────────────────────────────────────────┘
```

### 5.3 Interfaces

```python
# connectors/oauth_providers.py (NEW)

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Pre-configured OAuth endpoints for a well-known provider.

    Users only supply client_id and client_secret; everything else
    is known for each provider.
    """
    id: str
    display_name: str
    authorization_endpoint: str
    token_endpoint: str
    device_authorization_endpoint: str | None = None
    default_scopes: tuple[str, ...] = ()
    supports_pkce: bool = True
    discovery_url: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    docs_url: str = ""


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

OAUTH_PROVIDERS: dict[str, OAuthProviderConfig] = {}


def register_oauth_provider(config: OAuthProviderConfig) -> None:
    """Register a custom or override an existing provider."""
    OAUTH_PROVIDERS[config.id] = config


def get_oauth_provider(provider_id: str) -> OAuthProviderConfig | None:
    _ensure_builtins()
    return OAUTH_PROVIDERS.get(provider_id)


def list_oauth_providers() -> list[str]:
    _ensure_builtins()
    return sorted(OAUTH_PROVIDERS.keys())


# ---------------------------------------------------------------------------
# Built-in providers
# ---------------------------------------------------------------------------

_builtins_loaded = False

def _ensure_builtins() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True

    for config in _BUILTIN_PROVIDERS:
        OAUTH_PROVIDERS[config.id] = config


_BUILTIN_PROVIDERS = [
    OAuthProviderConfig(
        id="google",
        display_name="Google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        device_authorization_endpoint="https://oauth2.googleapis.com/device/code",
        default_scopes=(
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ),
        supports_pkce=True,
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        docs_url="https://developers.google.com/identity/protocols/oauth2",
    ),
    OAuthProviderConfig(
        id="google_gmail",
        display_name="Google (Gmail)",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        device_authorization_endpoint="https://oauth2.googleapis.com/device/code",
        default_scopes=(
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ),
        supports_pkce=True,
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        docs_url="https://developers.google.com/gmail/api/auth/scopes",
    ),
    OAuthProviderConfig(
        id="google_calendar",
        display_name="Google (Calendar)",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        device_authorization_endpoint="https://oauth2.googleapis.com/device/code",
        default_scopes=(
            "https://www.googleapis.com/auth/calendar",
        ),
        supports_pkce=True,
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    ),
    OAuthProviderConfig(
        id="atlassian",
        display_name="Atlassian (Jira/Confluence)",
        authorization_endpoint="https://auth.atlassian.com/authorize",
        token_endpoint="https://auth.atlassian.com/oauth/token",
        default_scopes=(
            "read:jira-work",
            "write:jira-work",
            "read:confluence-content.all",
        ),
        supports_pkce=True,
        extra_auth_params={"audience": "api.atlassian.com", "prompt": "consent"},
        docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
    ),
    OAuthProviderConfig(
        id="github",
        display_name="GitHub",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        device_authorization_endpoint="https://github.com/login/device/code",
        default_scopes=("repo", "read:user"),
        supports_pkce=False,
        docs_url="https://docs.github.com/en/apps/oauth-apps",
    ),
    OAuthProviderConfig(
        id="slack",
        display_name="Slack",
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        default_scopes=("chat:write", "channels:read"),
        supports_pkce=False,
        docs_url="https://api.slack.com/authentication/oauth-v2",
    ),
    OAuthProviderConfig(
        id="microsoft",
        display_name="Microsoft (Azure AD)",
        authorization_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        device_authorization_endpoint=(
            "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
        ),
        default_scopes=("openid", "profile", "email", "offline_access"),
        supports_pkce=True,
        docs_url="https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow",
    ),
    OAuthProviderConfig(
        id="linear",
        display_name="Linear",
        authorization_endpoint="https://linear.app/oauth/authorize",
        token_endpoint="https://api.linear.app/oauth/token",
        default_scopes=("read", "write"),
        supports_pkce=True,
    ),
    OAuthProviderConfig(
        id="notion",
        display_name="Notion",
        authorization_endpoint="https://api.notion.com/v1/oauth/authorize",
        token_endpoint="https://api.notion.com/v1/oauth/token",
        default_scopes=(),
        supports_pkce=False,
    ),
    OAuthProviderConfig(
        id="hubspot",
        display_name="HubSpot",
        authorization_endpoint="https://app.hubspot.com/oauth/authorize",
        token_endpoint="https://api.hubapi.com/oauth/v1/token",
        default_scopes=("crm.objects.contacts.read",),
        supports_pkce=False,
        docs_url="https://developers.hubspot.com/docs/api/oauth-quickstart-guide",
    ),
]
```

### 5.4 CLI Integration

```python
# cli/auth_commands.py — _resolve_target changes

def _resolve_target(args, *, name, env_prefix) -> _OAuthTarget:
    # NEW: check if name matches a known provider
    provider = get_oauth_provider(name)

    # Flags still override — a known provider is a default, not a mandate
    authorization_endpoint = (
        flag("authorization_endpoint")
        or os.environ.get(f"{env_prefix}_AUTHORIZATION_ENDPOINT")
        or (provider.authorization_endpoint if provider else None)
    )
    token_endpoint = (
        flag("token_endpoint")
        or os.environ.get(f"{env_prefix}_TOKEN_ENDPOINT")
        or (provider.token_endpoint if provider else None)
    )
    device_authorization_endpoint = (
        flag("device_authorization_endpoint")
        or os.environ.get(f"{env_prefix}_DEVICE_AUTHORIZATION_ENDPOINT")
        or (provider.device_authorization_endpoint if provider else None)
    )
    # Scopes merge: user-specified + provider defaults
    scopes = scope_flags or (
        tuple(os.environ.get(f"{env_prefix}_SCOPES", "").split())
        or (provider.default_scopes if provider else ())
    )
    ...
```

**New CLI flow:**
```bash
# Before (verbose):
loom connect google \
  --authorization-endpoint https://accounts.google.com/o/oauth2/v2/auth \
  --token-endpoint https://oauth2.googleapis.com/token \
  --client-id xxx --client-secret yyy \
  --scope "https://www.googleapis.com/auth/gmail.modify"

# After (simple):
loom connect google --client-id xxx --client-secret yyy

# List available providers:
loom providers

# Provider with extra scopes:
loom connect google_gmail --client-id xxx --client-secret yyy --scope "https://www.googleapis.com/auth/gmail.compose"
```

### 5.5 Custom Provider Registration

```python
# User code or plugin
from workflow_builder.connectors.oauth_providers import (
    OAuthProviderConfig,
    register_oauth_provider,
)

register_oauth_provider(OAuthProviderConfig(
    id="my_saas",
    display_name="My SaaS",
    authorization_endpoint="https://my.saas.com/oauth/authorize",
    token_endpoint="https://my.saas.com/oauth/token",
    default_scopes=("read", "write"),
))
```

### 5.6 OIDC Discovery Support

For providers that support OIDC, we can auto-discover endpoints:

```python
async def discover_oidc(issuer: str) -> OAuthProviderConfig:
    """Fetch the .well-known/openid-configuration and build a config."""
    import httpx
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    return OAuthProviderConfig(
        id=issuer.split("//")[1].split("/")[0].replace(".", "_"),
        display_name=issuer,
        authorization_endpoint=data["authorization_endpoint"],
        token_endpoint=data["token_endpoint"],
        device_authorization_endpoint=data.get("device_authorization_endpoint"),
        supports_pkce="S256" in data.get("code_challenge_methods_supported", []),
    )
```

---

## 6. Implementation Phases

### Phase A: Foundation — `UserInteraction` Protocol + `RunEnvironment` (Days 1-2)

| Step | Files | Description |
|------|-------|-------------|
| A1 | `connectors/interaction.py` (new) | `UserInteraction` protocol, `UserQuestion`, `UserResponse`, `CLIUserInteraction`, `CallbackUserInteraction` |
| A2 | `runtime/environment.py` (new) | `RunEnvironment` class, `current_run_environment()` ContextVar |
| A3 | `connectors/__init__.py` | Re-export new types |
| A4 | Tests | `tests/test_user_interaction.py`, `tests/test_run_environment.py` |

### Phase B: Credential Layering + Token Passing (Days 2-3)

| Step | Files | Description |
|------|-------|-------------|
| B1 | `connectors/credentials.py` | Add `LayeredCredentialStore` |
| B2 | `runtime/engine.py` | Add `credentials` param to `run()`, `submit()`. Build `LayeredCredentialStore` in `_drive_inner`. Add `env` param |
| B3 | `runtime/context.py` | Add `self.env: RunEnvironment` to Context. Bind `RunEnvironment` ContextVar in `_attempt_loop` |
| B4 | `toolsets/connections.py` | Add `tokens` param to `ConnectionBroker` |
| B5 | `facade.py` | Extend `LocalFacade.start()` / `RemoteFacade` to accept credentials and env |
| B6 | Tests | `tests/test_layered_credentials.py`, `tests/test_run_credentials.py`, `tests/test_run_env.py` |

### Phase C: Pre-configured OAuth Providers (Days 3-4)

| Step | Files | Description |
|------|-------|-------------|
| C1 | `connectors/oauth_providers.py` (new) | `OAuthProviderConfig`, registry, 9 built-in providers |
| C2 | `cli/auth_commands.py` | Integrate provider lookup in `_resolve_target`. Add `cmd_providers` |
| C3 | `cli/commands.py` | Register `providers` subcommand in argparse |
| C4 | `connectors/__init__.py` | Re-export `OAuthProviderConfig`, `register_oauth_provider` |
| C5 | Tests | `tests/test_oauth_providers.py`, `tests/test_cli_connect_providers.py` |

### Phase D: User Ask Tool Integration (Days 4-5)

| Step | Files | Description |
|------|-------|-------------|
| D1 | `agents/coding_tools.py` | Add `ask_user` tool, extend `build_coding_tools()` to accept and wire `UserInteraction` |
| D2 | `agents/coding_agent.py` | Add `user_interaction` param, pass to `build_coding_tools()` |
| D3 | `agents/agent.py` | Add optional `user_interaction` field, inject `ask_user` tool in `_resolve_executor()` |
| D4 | `agents/runner.py` | Thread `user_interaction` through `BuiltInAgentRuntime` |
| D5 | Tests | `tests/test_ask_user_tool.py`, `tests/test_coding_agent_ask.py` |

### Phase E: CLI + MCP + Facade Integration (Days 5-6)

| Step | Files | Description |
|------|-------|-------------|
| E1 | `cli/commands.py` | Add `--env` / `--env-file` to `cmd_run`. Wire `CLIUserInteraction` for agent commands |
| E2 | `facade.py` | Thread `credentials`, `env` through facade `start()` |
| E3 | `mcp_server/tools.py` (if exists) | Add env/credentials support to MCP run tool |
| E4 | `server.py` (API) | Accept credentials/env in POST `/runs` |
| E5 | Tests | `tests/test_cli_env.py`, `tests/test_facade_credentials.py` |

### Phase F: End-to-End Integration Tests (Day 6-7)

| Step | Files | Description |
|------|-------|-------------|
| F1 | `tests/test_e2e_token_passing.py` | Workflow with Google toolset, programmatic token, runs to completion |
| F2 | `tests/test_e2e_env_vars.py` | Workflow reads env var passed at run time, not from process env |
| F3 | `tests/test_e2e_ask_user.py` | CodingAgent with CallbackUserInteraction, answers questions, produces correct code |
| F4 | `tests/test_e2e_oauth_connect.py` | `loom connect google` with only client_id/secret succeeds |
| F5 | `tests/test_surface_parity.py` | Extend existing parity tests for new API surface |

---

## 7. Test Plan

### 7.1 Unit Tests

| Test | What it verifies |
|------|-----------------|
| `test_user_question_model` | `UserQuestion` serialization, validation |
| `test_cli_user_interaction` | `CLIUserInteraction` with mocked stdin |
| `test_callback_user_interaction` | Custom callback receives question, returns response |
| `test_ask_user_tool_budget` | Tool refuses after `max_questions` calls |
| `test_ask_user_disabled_in_repair` | Tool returns error during repair phase |
| `test_layered_credential_store` | Priority ordering: run > runtime > env |
| `test_layered_store_write` | Writes go to first layer |
| `test_layered_store_names` | Deduplicates across layers |
| `test_run_environment_precedence` | run_env > runtime_env > os.environ |
| `test_run_environment_isolation` | Two concurrent runs see different envs |
| `test_run_environment_mapping` | Implements Mapping correctly |
| `test_oauth_provider_registry` | Registration, lookup, listing |
| `test_builtin_providers_complete` | Every built-in has auth+token endpoints |
| `test_provider_override` | Flag overrides provider default |
| `test_provider_scopes_merge` | User scopes extend provider defaults |
| `test_oidc_discovery` | Auto-discovery from `.well-known` |
| `test_runtime_run_with_credentials` | `run(credentials={"x": "token"})` |
| `test_runtime_run_with_env` | `run(env={"KEY": "val"})` |
| `test_coding_agent_with_ask_user` | Agent has ask_user tool when interaction provided |
| `test_coding_agent_without_ask_user` | Agent omits ask_user tool when no interaction |

### 7.2 Integration Tests

| Test | What it verifies |
|------|-----------------|
| `test_credential_flows_through_step` | Token passed at `run()` reaches step body |
| `test_env_flows_through_step` | Env var passed at `run()` readable via `ctx.env` |
| `test_ask_user_in_coding_loop` | Full coding agent generation with ask_user interaction |
| `test_connect_with_provider` | `_resolve_target` uses provider config |
| `test_concurrent_runs_isolated_env` | Two runs with different env don't interfere |
| `test_concurrent_runs_isolated_creds` | Two runs with different creds don't interfere |

### 7.3 End-to-End Tests

| Test | What it verifies |
|------|-----------------|
| `test_e2e_workflow_with_injected_token` | Full workflow using programmatic token |
| `test_e2e_env_override` | Workflow behavior changes based on env |
| `test_e2e_coding_agent_asks_and_resolves` | Agent asks question, uses answer in code |
| `test_e2e_loom_connect_google` | CLI connects with just client_id/secret |

### 7.4 Conformance Tests

| Test | What it verifies |
|------|-----------------|
| `test_credential_store_conformance_layered` | `LayeredCredentialStore` passes existing conformance suite |
| `test_user_interaction_conformance` | All implementations satisfy protocol |

---

## 8. Multi-Angle Review

### 8.1 Correctness

- **Credential precedence is well-defined**: run-level > runtime-level > environment. No ambiguity
- **No journal leaks**: `LayeredCredentialStore` uses `Secret[str]` throughout. Tokens passed as `dict[str, str]` are wrapped in `Secret` at the boundary
- **ContextVar isolation**: Both `credential_store_scope` and `_current_env` use ContextVars, which are natively async-safe — concurrent runs on the same event loop each see their own bindings
- **Ask tool budget**: Hard limit prevents infinite question loops. Counter resets per `generate()` call

### 8.2 Security

- **Tokens never in journals**: `StoredCredential.token` is `Secret[str]` — serializing it raises `SerializationError`, not a leak
- **Tokens never in logs**: `Secret.__repr__` returns `"Secret(***)"`. The `LayeredCredentialStore` never logs the values it resolves
- **Env vars are not journaled**: `RunEnvironment` overrides are stored in `record.metadata` for provenance, not in the journal. On replay, the same overrides are re-applied — env vars are not determinism-affecting because they are re-applied, not replayed from a snapshot
- **OAuth provider registry is read-only metadata**: It contains no secrets — only public endpoint URLs. Client IDs and secrets remain in the `CredentialStore`, encrypted at rest
- **Ask tool is read-only**: It cannot modify state. The response goes through the model's context, not directly into generated code

### 8.3 Performance

- **Zero overhead when unused**: `credentials=None` and `env=None` at `run()` time skip layering entirely — the existing credential_store_scope binding is unchanged
- **No extra network calls**: The OAuth provider registry is a static dict, not a discovery service. OIDC discovery is opt-in and cached
- **RunEnvironment is a dict lookup**: O(1) per `get()`. No serialization overhead
- **LayeredCredentialStore**: Falls through layers sequentially. With 2-3 layers, this is negligible

### 8.4 Edge Cases

| Edge case | Handling |
|-----------|---------|
| `run(credentials={"google": ""})` | Empty string is a valid token (degenerate). The toolset's own validation catches it on use |
| Concurrent runs with same credential name, different values | ContextVar isolation — each run's `credential_store_scope` is independent |
| `ask_user` called with no options but `input_type="select"` | Validation in `UserQuestion` — requires options for select types |
| Provider config overridden partially | Explicit flags merge with provider defaults; no "all or nothing" |
| `env` key collides with a security-sensitive env var | `RunEnvironment` is scoped to the run, does not mutate `os.environ`. Other processes/runs are unaffected |
| Replay with different `env` than original run | `env` overrides from `record.metadata` are re-applied. If the caller changes them, behavior may differ — this is documented, not prevented, because env is explicitly non-deterministic (like credentials) |
| `ask_user` during smoke test | Returns error: "not available in this phase" — prevents blocking |

### 8.5 Maintainability

- **All new types are protocols or dataclasses**: No deep inheritance. `UserInteraction` is a one-method protocol, trivially implementable
- **Provider registry is data, not code**: Adding a new provider is one `OAuthProviderConfig` object. No behavioral changes needed
- **LayeredCredentialStore composes existing stores**: No new persistence logic. It delegates to stores that already pass the conformance suite
- **RunEnvironment is a `Mapping`**: Standard Python protocol — any code expecting a `Mapping[str, str]` works unchanged

### 8.6 User Perspective

**Before:**
```python
# Setting up credentials requires env vars or loom connect with many flags
os.environ["GOOGLE_ACCESS_TOKEN"] = token
rt = Runtime()
result = await rt.run(my_workflow, data)
```

**After:**
```python
# Simple: pass token directly
result = await rt.run(my_workflow, data, credentials={"google": token})

# Or: pass environment
result = await rt.run(my_workflow, data, env={"API_URL": "https://..."})

# Or: connect with just client_id/secret
# loom connect google --client-id xxx --client-secret yyy

# Or: get a smarter coding agent that asks when confused
agent = WorkflowCodingAgent(model=provider, user_interaction=CLIUserInteraction())
result = await agent.generate("Build a workflow that...")
```

### 8.7 Backward Compatibility

- **All new parameters are optional with `None` defaults**: Existing code is untouched
- **Existing credential resolution unchanged**: Environment variable fallback still works. `credential_store_scope` still binds the same way
- **`loom connect` flags still work**: Provider config provides defaults; explicit flags override
- **No public API removals**: Only additions to `Runtime.run()`, `WorkflowCodingAgent.__init__`, `ConnectionBroker.__init__`

---

## 9. File Change Inventory

### New Files

| File | Purpose |
|------|---------|
| `src/workflow_builder/connectors/interaction.py` | `UserInteraction` protocol, `UserQuestion`, `UserResponse`, `CLIUserInteraction`, `CallbackUserInteraction`, `SuspendUserInteraction` |
| `src/workflow_builder/connectors/oauth_providers.py` | `OAuthProviderConfig`, provider registry, 9 built-in providers, `discover_oidc()` |
| `src/workflow_builder/runtime/environment.py` | `RunEnvironment`, `current_run_environment()` ContextVar |
| `tests/test_user_interaction.py` | Unit tests for UserInteraction implementations |
| `tests/test_run_environment.py` | Unit tests for RunEnvironment |
| `tests/test_layered_credentials.py` | Unit tests for LayeredCredentialStore |
| `tests/test_oauth_providers.py` | Unit tests for OAuth provider registry |
| `tests/test_run_credentials.py` | Integration: credentials passed at run() time |
| `tests/test_run_env.py` | Integration: env vars passed at run() time |
| `tests/test_ask_user_tool.py` | Unit tests for ask_user tool |
| `tests/test_e2e_token_passing.py` | End-to-end: programmatic token injection |
| `tests/test_e2e_env_vars.py` | End-to-end: runtime env var injection |
| `tests/test_e2e_ask_user.py` | End-to-end: coding agent with ask_user |
| `tests/test_e2e_oauth_connect.py` | End-to-end: loom connect with pre-configured provider |

### Modified Files

| File | Changes |
|------|---------|
| `src/workflow_builder/connectors/__init__.py` | Re-export `UserInteraction`, `UserQuestion`, `UserResponse`, `CLIUserInteraction`, `CallbackUserInteraction`, `OAuthProviderConfig`, `register_oauth_provider` |
| `src/workflow_builder/connectors/credentials.py` | Add `LayeredCredentialStore` class |
| `src/workflow_builder/runtime/engine.py` | Add `env` to `Runtime.__init__`. Add `credentials`, `env` to `run()` and `submit()`. Build layered store in `_drive_inner` |
| `src/workflow_builder/runtime/context.py` | Add `self.env: RunEnvironment`. Bind env ContextVar in `_attempt_loop`. Import `RunEnvironment` |
| `src/workflow_builder/toolsets/connections.py` | Add `tokens` param to `ConnectionBroker.__init__` and priority lookup |
| `src/workflow_builder/agents/coding_agent.py` | Add `user_interaction` param to `WorkflowCodingAgent.__init__`, pass to `build_coding_tools()` |
| `src/workflow_builder/agents/coding_tools.py` | Add `ask_user` tool. Extend `build_coding_tools()` signature with `user_interaction` param |
| `src/workflow_builder/agents/agent.py` | Add optional `user_interaction` field to `Agent` |
| `src/workflow_builder/agents/runner.py` | Thread `user_interaction` to inject `ask_user` tool |
| `src/workflow_builder/cli/auth_commands.py` | Integrate `get_oauth_provider()` in `_resolve_target`. Add `cmd_providers` |
| `src/workflow_builder/cli/commands.py` | Add `--env` / `--env-file` to run parser. Register `providers` subcommand |
| `src/workflow_builder/facade.py` | Extend `LocalFacade.start()` to accept `credentials` and `env` |
| `src/workflow_builder/toolsets/google/auth.py` | Use `current_run_environment()` in `GoogleCredentials.from_env()` as fallback |
| `src/workflow_builder/__init__.py` | Export `RunEnvironment`, `UserInteraction` (if public API desired) |

### Dependency Notes

- No new pip dependencies. All implementations use stdlib + existing deps (httpx already available, pydantic already available)
- `rich` remains optional for `CLIUserInteraction` (falls back to `input()`)

---

## Appendix A: Directory Structure (New/Modified)

```
src/workflow_builder/
├── __init__.py                          # MODIFIED: export new types
├── connectors/
│   ├── __init__.py                      # MODIFIED: re-exports
│   ├── credentials.py                   # MODIFIED: +LayeredCredentialStore
│   ├── interaction.py                   # NEW: UserInteraction protocol
│   ├── oauth_client.py                  # (unchanged)
│   ├── oauth_providers.py              # NEW: Pre-configured providers
│   └── encryption.py                    # (unchanged)
├── runtime/
│   ├── engine.py                        # MODIFIED: +credentials, +env on run()
│   ├── context.py                       # MODIFIED: +self.env, bind env ContextVar
│   └── environment.py                   # NEW: RunEnvironment
├── agents/
│   ├── agent.py                         # MODIFIED: +user_interaction
│   ├── coding_agent.py                  # MODIFIED: +user_interaction
│   ├── coding_tools.py                  # MODIFIED: +ask_user tool
│   └── runner.py                        # MODIFIED: thread user_interaction
├── toolsets/
│   ├── connections.py                   # MODIFIED: +tokens param
│   └── google/
│       └── auth.py                      # MODIFIED: use run environment
├── cli/
│   ├── auth_commands.py                 # MODIFIED: provider lookup
│   └── commands.py                      # MODIFIED: --env, providers cmd
└── facade.py                            # MODIFIED: credentials/env in start()

tests/
├── test_user_interaction.py             # NEW
├── test_run_environment.py              # NEW
├── test_layered_credentials.py          # NEW
├── test_oauth_providers.py              # NEW
├── test_run_credentials.py              # NEW
├── test_run_env.py                      # NEW
├── test_ask_user_tool.py                # NEW
├── test_e2e_token_passing.py            # NEW
├── test_e2e_env_vars.py                 # NEW
├── test_e2e_ask_user.py                 # NEW
└── test_e2e_oauth_connect.py            # NEW
```

## Appendix B: Key Code Snippets (Full Implementation Previews)

### B.1 CLIUserInteraction (complete)

```python
class CLIUserInteraction:
    """Default interactive implementation: reads from stdin.

    Uses rich.prompt when available (the [cli] extra), falls back to
    builtins.input on a bare install. Both paths handle the structured
    question model — options become a numbered list, confirm becomes
    yes/no, select validates against the options.
    """

    async def ask(self, question: UserQuestion) -> UserResponse:
        import asyncio
        return await asyncio.to_thread(self._ask_sync, question)

    def _ask_sync(self, question: UserQuestion) -> UserResponse:
        import sys
        if question.context:
            print(f"\n  Context: {question.context}", file=sys.stderr)

        if question.input_type == "confirm":
            answer = self._confirm(question.question, question.default)
            return UserResponse(answer=answer)
        elif question.input_type in ("select", "multiselect") and question.options:
            answer = self._select(question)
            return UserResponse(answer=answer)
        else:
            answer = self._text(question.question, question.default)
            if not answer and question.allow_skip:
                return UserResponse(answer="", skipped=True)
            return UserResponse(answer=answer)

    def _text(self, prompt: str, default: str | None) -> str:
        suffix = f" [{default}]" if default else ""
        try:
            from rich.prompt import Prompt
            return Prompt.ask(f"\n  🤖 {prompt}{suffix}") or default or ""
        except ImportError:
            return input(f"\n  Agent asks: {prompt}{suffix}\n  > ") or default or ""

    def _confirm(self, prompt: str, default: str | None) -> str:
        try:
            from rich.prompt import Confirm
            return "yes" if Confirm.ask(f"\n  🤖 {prompt}") else "no"
        except ImportError:
            answer = input(f"\n  Agent asks: {prompt} [y/n] > ").strip().lower()
            return "yes" if answer in ("y", "yes") else "no"

    def _select(self, question: UserQuestion) -> str:
        options = question.options or []
        print(f"\n  🤖 {question.question}")
        for i, opt in enumerate(options, 1):
            print(f"    {i}. {opt}")
        while True:
            raw = input("  > ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            if raw in options:
                return raw
            print(f"  Please enter 1-{len(options)} or the option text.")
```

### B.2 Runtime.run() with new params (preview)

```python
async def run(
    self,
    target: WorkflowDefinition[Any, Any, Any] | str,
    input: Any = None,
    *,
    deps: Any = None,
    credentials: dict[str, str] | CredentialStore | None = None,
    env: dict[str, str] | None = None,
    run_id: str | None = None,
    trigger: TriggerKind = TriggerKind.MANUAL,
    idempotency_key: str | None = None,
    parent_run_id: str | None = None,
    root_run_id: str | None = None,
    tags: Sequence[str] = (),
    metadata: dict[str, Any] | None = None,
) -> ExecutionResult:
    ...
    record = ExecutionRecord(...)
    # Store env for provenance (not in journal)
    if env:
        record.metadata["run_env"] = env
    await self.store.create_execution(record)
    return await self._drive(
        record.run_id, deps=deps,
        run_credentials=credentials, run_env=env,
    )
```
