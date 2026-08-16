"""HTTP surface for a :class:`Runtime`.

Workflow *authoring* is Python — the durability guarantees come from re-entering
a Python function body, and that does not survive a language boundary. Workflow
*operation* does not need to be: starting runs, delivering events, and reading
history are ordinary requests. Putting them behind HTTP is what lets a Go
service start a workflow or a TypeScript UI watch one, without either of them
embedding a Python interpreter.

Every route here delegates to a :class:`~loom.facade.RuntimeFacade`,
the same port the CLI and the MCP server hold. That is not indirection for its
own sake: three surfaces over one Runtime is three places to add each new
capability unless they share an abstraction, and the drift shows up as an
operation that works in the CLI and 404s over HTTP.

Requires the ``api`` extra::

    pip install loomflow[api]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from loom.core.exceptions import (
    AdmissionRejected,
    ConfigurationError,
    InputMismatch,
    InsufficientScope,
    RegistryError,
)
from loom.core.models import ExecutionStatus
from loom.events.ingress import WebhookIngress
from loom.events.sources import MalformedDelivery, VerificationFailed
from loom.facade import LocalFacade, RuntimeFacade
from loom.identity.principal import Principal
from loom.security.rbac import AuthorizationError
from loom.server.auth import (
    build_http_auth,
    build_principal_dependency,
    mount_protected_resource_metadata,
)
from loom.triggers.specs import Webhook

if TYPE_CHECKING:
    from loom.identity.config import IdentitySettings
    from loom.runtime.engine import Runtime


class StartRunRequest(BaseModel):
    """Body for ``POST /runs``."""

    workflow: str
    input: Any = None
    idempotency_key: str | None = None
    """Reuse to make a retried request return the original run instead of a new one."""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    wait: bool = False
    """Block until the run reaches a terminal state or parks. Off by default,
    because a workflow that sleeps for a day should not hold a socket open."""
    env: dict[str, str] | None = None
    """Per-run environment overrides. Not for secrets — use ``loom connect``."""
    credentials: dict[str, str] | None = None
    """Refused by AuthorizedFacade / RemoteFacade; LocalFacade honours it."""

    @field_validator("metadata")
    @classmethod
    def _drop_reserved_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {"loom.env", "loom.credential_names"}
        return {k: v for k, v in value.items() if k not in reserved}


class EventRequest(BaseModel):
    """Body for ``POST /runs/{run_id}/events``."""

    name: str
    payload: Any = None


class ConfirmUploadRequest(BaseModel):
    """Body for ``POST /artifacts/{name}/confirm``."""

    upload_id: str
    run_id: str = ""
    metadata: dict[str, Any] | None = None


class PutArtifactRequest(BaseModel):
    """Body for ``POST /artifacts/{name}`` — small payloads only."""

    content_b64: str
    mime: str = "application/octet-stream"
    metadata: dict[str, Any] | None = None


class UploadUrlRequest(BaseModel):
    """Body for ``POST /artifacts/{name}/upload-url``."""

    mime: str = "application/octet-stream"
    max_size: int | None = None
    expires_in: int | None = None


class RunView(BaseModel):
    """A run as seen over the wire."""

    run_id: str
    workflow: str
    status: ExecutionStatus
    input: Any = None
    output: Any = None
    error: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Round-tripped so an ``AuthorizedFacade`` (identity/facade.py) can read
    a run's pinned owner the same way over HTTP as it does in-process —
    without this, ownership checks would only work when wrapping a
    ``LocalFacade`` directly."""


class WorkflowView(BaseModel):
    """A workflow as seen over the wire."""

    name: str
    version: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    """``None`` when no schema can be derived — a workflow that takes no input,
    or one whose annotation is not expressible as JSON Schema. Distinct from
    ``{}``, which would claim a shape with no fields; a caller that cannot tell
    those apart has to guess, which is the thing publishing a schema prevents."""
    triggers: list[str] = Field(default_factory=list)
    executable: bool = True
    """Whether *this* process can run it. A published workflow that the serving
    process did not import is listed but cannot be started here — better to say
    so than to omit it and look like it does not exist."""
    code_hash: str = ""
    source_file: str = ""


def _view(run: dict[str, Any]) -> RunView:
    """A facade run dict as the HTTP view.

    ``RunView`` names a subset of the facade's keys, so this is a projection
    rather than a translation — nothing is renamed on the way through.
    """
    return RunView.model_validate(run)


def _credential_lifespan(facade: RuntimeFacade) -> Any:
    """Sweep stored OAuth credentials while the server is up, or ``None``.

    ``None`` — no lifespan at all — whenever this process has no credential
    store to sweep, which is every ``Runtime()`` that did not configure one.
    That is deliberate: a server with nothing to refresh should start no
    background task, so an app built in a test behaves exactly as it did
    before this existed.

    A long-lived server is the case that needs this most. Renewal on next use
    covers a CLI command; it does not cover a process that stays up for a week
    and touches a credential twice, where the second touch is the one that
    discovers the token expired on Tuesday.
    """
    runtime = getattr(facade, "runtime", None)
    if runtime is None or getattr(runtime, "credentials", None) is None:
        return None

    import contextlib
    from collections.abc import AsyncIterator

    from loom.connectors.refresh import service_for

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        service = service_for(runtime)
        if service is not None:
            await service.start()
        try:
            yield
        finally:
            if service is not None:
                await service.stop()

    return lifespan


def create_app(
    runtime: Runtime | RuntimeFacade,
    *,
    title: str = "LOOM",
    identity: IdentitySettings | None = None,
) -> FastAPI:
    """Build a FastAPI app serving *runtime*.

    Accepts a :class:`Runtime` (wrapped in a :class:`LocalFacade`) or any
    :class:`RuntimeFacade` — including a :class:`RemoteFacade`, which makes this
    app a proxy in front of another LOOM server.

    The app owns no state of its own — every route delegates to the facade, so
    what a client sees over HTTP and what embedded Python sees are the same
    execution history rather than two views that can drift.

    *identity* defaults to ``IdentitySettings()`` (read from ``LOOM_AUTH_*``
    env vars). With none set, ``is_configured()`` is ``False``,
    :func:`~loom.server.auth.build_http_auth` returns ``None``,
    and every request is served by *base* directly — the compatibility
    contract: an install that sets no ``LOOM_AUTH_*`` var gets exactly the
    same unauthenticated app this function built before this parameter
    existed.
    """
    from loom.identity.config import IdentitySettings as _IdentitySettings
    from loom.runtime.engine import Runtime as _Runtime

    # Tested against the concrete class rather than the protocol: RuntimeFacade
    # is runtime_checkable, which compares attribute *names* only, and a Runtime
    # shares enough of them for that check to be a coin toss.
    base: RuntimeFacade = (
        LocalFacade(runtime) if isinstance(runtime, _Runtime) else runtime
    )
    identity = identity if identity is not None else _IdentitySettings()
    http_auth = build_http_auth(identity)
    principal_dependency = build_principal_dependency(http_auth)

    app = FastAPI(title=title, lifespan=_credential_lifespan(base))
    if http_auth is not None:
        mount_protected_resource_metadata(app, http_auth)

    #: Assigned once, for the same B008 reason as `injected` below.
    principal_injected = Depends(principal_dependency)

    async def _facade(principal: Principal = principal_injected) -> RuntimeFacade:
        """The facade a request acts through.

        A dependency rather than a closure so a host subclassing this app — or
        overriding it with ``app.dependency_overrides`` — has one place to
        change what a request is served by. Wrapped in an ``AuthorizedFacade``
        only when identity is configured, per the compatibility contract above
        — every route below asks for this and needs no other change to gain
        (or not gain) authorization.
        """
        if http_auth is None:
            return base
        from loom.identity.facade import AuthorizedFacade

        return AuthorizedFacade(base, principal)

    #: What every route asks for instead of closing over one shared facade.
    #: Spelled as a default rather than ``Annotated``: this module uses
    #: postponed annotations, and FastAPI resolves those against the *module*
    #: namespace, so an alias defined inside this function would never be found
    #: and the parameter would silently become a query string instead.
    injected = Depends(_facade)

    def _fail(exc: Exception) -> HTTPException:
        """Translate SDK errors into the status codes they actually mean."""
        if isinstance(exc, InsufficientScope):
            # 403, not 401: the caller authenticated fine, this token just
            # never held enough to do this — retrying the same login again
            # would not help, which is what 401 would otherwise imply.
            return HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "detail": str(exc),
                    "required": exc.required,
                },
            )
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, InputMismatch):
            # 422, not 400: the request parsed, and the payload is
            # well-formed JSON — it just is not what this workflow accepts.
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, ConfigurationError):
            return HTTPException(status_code=400, detail=str(exc))
        from loom.blobs.artifact import ArtifactNotFound
        from loom.blobs.blob import BlobNotFoundError
        from loom.blobs.signed_urls import UploadNotFound, UploadTooLarge
        from loom.blobs.staging import StagingNotFound

        if isinstance(
            exc, ArtifactNotFound | UploadNotFound | StagingNotFound | BlobNotFoundError
        ):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, UploadTooLarge):
            return HTTPException(status_code=413, detail=str(exc))
        if isinstance(exc, RegistryError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, AdmissionRejected):
            # 429 for "come back later", 409 for "this will never be admitted".
            code = 429 if exc.retryable else 409
            return HTTPException(status_code=code, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _require(facade: RuntimeFacade, run_id: str) -> dict[str, Any]:
        """The run, or a 404. Every run-scoped route starts here."""
        try:
            found = await facade.get(run_id)
        except Exception as exc:
            raise _fail(exc) from exc
        if found is None:
            raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
        return found

    @app.get("/workflows", response_model=list[WorkflowView])
    async def list_workflows(
        published: bool = True, facade: RuntimeFacade = injected
    ) -> list[WorkflowView]:
        """Workflows this process imported, plus published ones when asked.

        Set ``published=false`` to see only what can be started here.
        """
        try:
            entries = await facade.workflows(published=published)
        except Exception as exc:
            raise _fail(exc) from exc
        return [WorkflowView.model_validate(entry) for entry in entries]

    @app.post("/runs", response_model=RunView, status_code=202)
    async def start_run(body: StartRunRequest, facade: RuntimeFacade = injected) -> RunView:
        try:
            run = await facade.start(
                body.workflow,
                body.input,
                idempotency_key=body.idempotency_key,
                tags=body.tags,
                metadata=body.metadata,
                wait=body.wait,
                env=body.env,
                credentials=body.credentials,
            )
        except Exception as exc:
            raise _fail(exc) from exc
        return _view(run)

    @app.get("/runs", response_model=list[RunView])
    async def list_runs(
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        limit: int = 50,
        facade: RuntimeFacade = injected,
    ) -> list[RunView]:
        try:
            runs = await facade.list_runs(
                workflow=workflow,
                status=status.value if status else None,
                limit=limit,
            )
        except Exception as exc:
            raise _fail(exc) from exc
        return [_view(run) for run in runs]

    @app.get("/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str, facade: RuntimeFacade = injected) -> RunView:
        return _view(await _require(facade, run_id))

    @app.get("/runs/{run_id}/journal")
    async def get_journal(run_id: str, facade: RuntimeFacade = injected) -> list[dict[str, Any]]:
        await _require(facade, run_id)
        try:
            return await facade.journal(run_id)
        except Exception as exc:
            raise _fail(exc) from exc

    @app.get("/runs/{run_id}/reports")
    async def get_reports(
        run_id: str, offset: int = 0, facade: RuntimeFacade = injected
    ) -> list[dict[str, Any]]:
        """What the run has said about itself while running."""
        await _require(facade, run_id)
        try:
            return await facade.reports(run_id, offset)
        except Exception as exc:
            raise _fail(exc) from exc

    @app.post("/runs/{run_id}/events", status_code=202)
    async def send_event(
        run_id: str, body: EventRequest, facade: RuntimeFacade = injected
    ) -> dict[str, Any]:
        await _require(facade, run_id)
        try:
            await facade.send_event(run_id, body.name, body.payload)
        except Exception as exc:
            raise _fail(exc) from exc
        return {"run_id": run_id, "event": body.name, "delivered": True}

    @app.post("/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(run_id: str, facade: RuntimeFacade = injected) -> RunView:
        await _require(facade, run_id)
        try:
            return _view(await facade.cancel(run_id))
        except Exception as exc:
            raise _fail(exc) from exc

    @app.post("/runs/{run_id}/retry", response_model=RunView)
    async def retry_run(run_id: str, facade: RuntimeFacade = injected) -> RunView:
        """Re-run a failed execution from its first failed step.

        Distinct from replay: replay rehearses against the recorded journal and
        repeats nothing, while this prunes the failure and does the work again
        against current code.
        """
        await _require(facade, run_id)
        try:
            await facade.retry(run_id)
        except Exception as exc:
            raise _fail(exc) from exc
        return _view(await _require(facade, run_id))

    @app.post("/runs/{run_id}/replay", response_model=RunView)
    async def replay_run(run_id: str, facade: RuntimeFacade = injected) -> RunView:
        await _require(facade, run_id)
        try:
            replayed = await facade.replay(run_id)
        except Exception as exc:
            raise _fail(exc) from exc
        # Replay starts a *new* run; report that one, not the original.
        return _view(await _require(facade, replayed["run_id"]))

    @app.get("/artifacts")
    async def list_artifacts(facade: RuntimeFacade = injected) -> list[dict[str, Any]]:
        try:
            return await facade.list_artifacts()
        except Exception as exc:
            raise _fail(exc) from exc

    @app.get("/artifacts/{name}")
    async def get_artifact(name: str, facade: RuntimeFacade = injected) -> dict[str, Any]:
        try:
            history = await facade.artifact_history(name)
        except Exception as exc:
            raise _fail(exc) from exc
        if not history:
            raise HTTPException(status_code=404, detail=f"no artifact named {name!r}")
        return history[-1]

    @app.get("/artifacts/{name}/versions")
    async def artifact_versions(
        name: str, facade: RuntimeFacade = injected
    ) -> list[dict[str, Any]]:
        try:
            return await facade.artifact_history(name)
        except Exception as exc:
            raise _fail(exc) from exc

    @app.get("/artifacts/{name}/url")
    async def artifact_url(
        name: str,
        version: int | None = None,
        expires_in: int = 3600,
        facade: RuntimeFacade = injected,
    ) -> dict[str, Any]:
        try:
            return await facade.artifact_url(name, version, expires_in)
        except Exception as exc:
            raise _fail(exc) from exc

    @app.get("/artifacts/{name}/content")
    async def artifact_content(
        name: str,
        version: int | None = None,
        facade: RuntimeFacade = injected,
    ) -> dict[str, Any]:
        try:
            return await facade.read_artifact(name, version)
        except Exception as exc:
            raise _fail(exc) from exc

    @app.get("/artifacts/{name}/download")
    async def download_artifact(
        name: str,
        version: int | None = None,
        facade: RuntimeFacade = injected,
    ) -> Any:
        import base64

        try:
            info = await facade.artifact_url(name, version)
        except ConfigurationError:
            try:
                payload = await facade.read_artifact(name, version)
            except Exception as exc:
                raise _fail(exc) from exc
            headers: dict[str, str] = {}
            disposition = payload.get("content_disposition") or name
            if "filename=" not in str(disposition):
                disposition = f'attachment; filename="{disposition}"'
            headers["Content-Disposition"] = str(disposition)
            return Response(
                content=base64.b64decode(payload["content_b64"]),
                media_type=payload.get("mime") or "application/octet-stream",
                headers=headers,
            )
        except Exception as exc:
            raise _fail(exc) from exc
        return RedirectResponse(info["url"], status_code=302)

    @app.post("/artifacts/{name}")
    async def put_artifact(
        name: str, body: PutArtifactRequest, facade: RuntimeFacade = injected
    ) -> dict[str, Any]:
        try:
            return await facade.put_artifact(
                name, body.content_b64, mime=body.mime, metadata=body.metadata
            )
        except Exception as exc:
            raise _fail(exc) from exc

    @app.post("/artifacts/{name}/upload-url")
    async def create_upload_url(
        name: str, body: UploadUrlRequest, facade: RuntimeFacade = injected
    ) -> dict[str, Any]:
        try:
            return await facade.upload_url(
                name, mime=body.mime, max_size=body.max_size, expires_in=body.expires_in
            )
        except Exception as exc:
            raise _fail(exc) from exc

    @app.post("/artifacts/{name}/confirm")
    async def confirm_artifact_upload(
        name: str, body: ConfirmUploadRequest, facade: RuntimeFacade = injected
    ) -> dict[str, Any]:
        try:
            return await facade.confirm_upload(
                body.upload_id, name, run_id=body.run_id, metadata=body.metadata
            )
        except Exception as exc:
            raise _fail(exc) from exc

    @app.get("/blobs/{ref:path}")
    async def download_blob(
        ref: str,
        expires: int,
        sig: str,
        method: str = "GET",
        facade: RuntimeFacade = injected,
    ) -> Response:
        import base64

        try:
            payload = await facade.read_blob(ref, expires, sig, method)
        except Exception as exc:
            raise _fail(exc) from exc
        return Response(
            content=base64.b64decode(payload["content_b64"]),
            media_type=payload.get("mime") or "application/octet-stream",
        )

    @app.put("/blobs/{ref:path}")
    async def upload_blob(
        ref: str,
        request: Request,
        expires: int,
        sig: str,
        method: str = "PUT",
        facade: RuntimeFacade = injected,
    ) -> dict[str, Any]:
        import base64

        body = await request.body()
        mime = request.headers.get("content-type") or "application/octet-stream"
        try:
            return await facade.write_blob(
                ref,
                expires,
                sig,
                base64.b64encode(body).decode("ascii"),
                mime=mime,
                method=method,
            )
        except Exception as exc:
            raise _fail(exc) from exc

    # -- ingress ------------------------------------------------------------
    #
    # Two routes, because two contracts exist and neither can be dropped.
    # `/webhook{path}` is what `Webhook.describe()` has been publishing all
    # along — an advertised URL is a promise, and providers are configured
    # against it. `/hooks/{source}` is the provider-typed one, where LOOM knows
    # who is calling and can therefore verify a signature, answer a handshake,
    # and fan one delivery out to every subscriber instead of to one workflow.
    #
    # Both are thirty lines over a transport-free core, so a host on Lambda or
    # a Django view calls the same `WebhookIngress.receive` and cannot drift
    # from what this serves.

    concrete: _Runtime | None = runtime if isinstance(runtime, _Runtime) else None

    @app.post("/hooks/{source_id}")
    async def receive_hook(source_id: str, request: Request) -> Response:
        """Accept a provider delivery, verify it, and append it to the log.

        Answers before any workflow runs, deliberately: Slack retries anything
        slower than three seconds, and a dispatcher that had to start runs
        inline would turn a slow workflow into a duplicate delivery. The append
        *is* the durable accept — everything after it is the log's problem, and
        the log survives this process dying one line later.
        """
        ingress = _ingress_or_503(concrete)
        body = await request.body()
        try:
            result = await ingress.receive(source_id, dict(request.headers), body)
        except VerificationFailed as exc:
            # 401 and never retried. Distinguished from 400 because the two mean
            # different things to whoever is watching the dashboard: this one is
            # somebody lying about who they are.
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except MalformedDelivery as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if result.challenge is not None:
            # Verbatim, with the provider's own content type. Slack wants the
            # bare challenge string and will not enable the endpoint for
            # anything else, JSON-wrapped included.
            return Response(
                content=result.challenge.body,
                media_type=result.challenge.content_type,
                status_code=result.challenge.status,
            )
        return JSONResponse(
            status_code=202,
            content={
                "accepted": result.accepted,
                "events": result.event_ids,
                "topics": result.topics,
                "reason": result.reason,
            },
        )

    async def _fire_webhook(path: str, request: Request, test: bool) -> JSONResponse:
        """Start every workflow whose ``Webhook`` trigger matches *path*."""
        if concrete is None:
            raise HTTPException(
                status_code=503,
                detail="webhook triggers need a local Runtime; this app is a "
                "proxy in front of another server, which serves them itself",
            )
        body = await request.body()
        payload = _webhook_payload(body, request)
        headers = {k.lower(): v for k, v in request.headers.items()}
        started: list[str] = []

        for definition in concrete.workflows.values():
            for spec in getattr(definition, "triggers", ()):
                if not isinstance(spec, Webhook) or spec.path != path:
                    continue
                if request.method.upper() not in {m.upper() for m in spec.methods}:
                    continue
                key = (
                    headers.get(spec.idempotency_header.lower())
                    if spec.idempotency_header
                    else None
                )
                run_id = await concrete.submit(
                    definition.name,
                    payload,
                    idempotency_key=key,
                    metadata={
                        "loom.webhook_path": path,
                        "loom.webhook_test": test,
                    },
                )
                started.append(run_id)

        if not started:
            # 404 rather than a quiet 202: a provider pointed at a path nothing
            # listens on looks identical to a working integration otherwise, and
            # is discovered when somebody asks why nothing happened.
            raise HTTPException(
                status_code=404,
                detail=f"no workflow declares a Webhook trigger on '{path}'",
            )
        return JSONResponse(status_code=202, content={"runs": started})

    # Test first, and the order is load-bearing: `{path:path}` is greedy, so
    # `/webhook{path:path}` registered ahead of this one matches
    # `/webhook-test/x` with path="-test/x", finds no trigger, and 404s. The
    # test URL would silently stop working — which is the failure it exists to
    # prevent, since the alternative is pointing a provider at production.
    @app.api_route("/webhook-test{path:path}", methods=["POST", "GET", "PUT"])
    async def webhook_test(path: str, request: Request) -> JSONResponse:
        """The test URL ``Webhook.describe()`` advertises, kept separate so
        that pointing a provider at a laptop cannot fire production runs."""
        return await _fire_webhook(path, request, test=True)

    @app.api_route("/webhook{path:path}", methods=["POST", "GET", "PUT"])
    async def webhook(path: str, request: Request) -> JSONResponse:
        """The production URL `Webhook.describe()` advertises."""
        return await _fire_webhook(path, request, test=False)

    return app


def _ingress_or_503(runtime: Runtime | None) -> WebhookIngress:
    """The ingress for this app, or a 503 saying exactly what is missing."""
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="provider ingress needs a local Runtime; this app is a proxy "
            "in front of another server, which serves /hooks itself",
        )
    existing = getattr(runtime, "_ingress", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    if getattr(runtime, "events", None) is None:
        raise HTTPException(
            status_code=503,
            detail="no event log is configured, so a delivery cannot be "
            "durably recorded and accepting one would lose it. Construct the "
            "Runtime with events=StoreBackedEventLog(store).",
        )
    built = WebhookIngress(runtime)
    runtime._ingress = built  # type: ignore[attr-defined]
    return built


def _webhook_payload(body: bytes, request: Request) -> dict[str, Any]:
    """A `Webhook` trigger's input: the decoded body, plus what it arrived with.

    Headers and query are carried alongside rather than merged, because a
    provider that posts a field called ``headers`` would otherwise overwrite
    them and the workflow would read the wrong thing with nothing to notice.
    """
    import json as _json

    decoded: Any
    try:
        decoded = _json.loads(body) if body else {}
    except ValueError:
        decoded = {"body": body.decode("utf-8", errors="replace")}
    return {
        "body": decoded,
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "query": dict(request.query_params),
        "method": request.method,
    }
