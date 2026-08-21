"""Thin HTTP client for a LOOM server.

Mirrors the shape of :class:`Runtime` for the operations that cross a process
boundary. Ports of this to other languages are a few dozen lines each — that is
the point of having the boundary at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loom.core.models import ExecutionStatus
from loom.core.types import Duration, to_seconds

TokenProvider = Callable[[bool], Awaitable["str | None"]]
"""Called with ``force_refresh`` before a request (``False``) and, once, again
after a 401 (``True``). A caller backed by a
:class:`~loom.connectors.credentials.CredentialStore` can wire
this straight to ``credentials.get(name)`` — ``force_refresh`` exists for a
401 that arrives *before* the stored credential's own ``expires_at``
(revocation, clock skew), which a plain ``get()`` would not otherwise retry."""


class LoomClientError(Exception):
    """A LOOM server returned an error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        www_authenticate: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.www_authenticate = www_authenticate
        """The ``WWW-Authenticate`` header from a 401, if any — carries the
        ``resource_metadata`` URL a caller needs to start a fresh login
        rather than just retry the same rejected token."""

    @property
    def retryable(self) -> bool:
        """Whether the same request could succeed later.

        429 is flow control asking for patience; 5xx is usually transient.
        """
        return self.status_code == 429 or self.status_code >= 500

    @property
    def requires_reauth(self) -> bool:
        """401 (bad/expired/missing token) or 403 ``insufficient_scope`` — both
        mean "log in again with more", never "try the exact same call again"."""
        return self.status_code in (401, 403)


def _as_object(payload: Any, path: str) -> dict[str, Any]:
    """Narrow a decoded JSON body to an object, or say who broke the contract.

    ``response.json()`` is untyped, so handing it straight back from a method
    declared to return a mapping asserts a shape nothing has checked. When the
    server answers with something else — most often a reverse proxy's HTML
    error page arriving with a 200, or a route that changed shape — the failure
    surfaces two layers down as ``'str' object has no attribute 'get'`` inside
    the CLI, which reads like a client bug rather than a server that answered
    wrongly. Failing here names the path and the type that actually arrived.
    """
    if not isinstance(payload, dict):
        raise LoomClientError(
            f"{path} answered with {type(payload).__name__}, expected a JSON object",
            status_code=502,
        )
    return payload


def _as_array(payload: Any, path: str) -> list[dict[str, Any]]:
    """The list counterpart of :func:`_as_object`.

    Elements are checked too, because every caller of a list-returning method
    here immediately indexes or ``.get()``s the rows — a list of strings passes
    a bare ``isinstance(payload, list)`` and fails identically one frame later.
    """
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise LoomClientError(
            f"{path} answered with {type(payload).__name__}, "
            "expected a JSON array of objects",
            status_code=502,
        )
    return payload


class LoomClient:
    """Async client for the LOOM HTTP API.

    Either pass a ``httpx.AsyncClient`` (handy for tests against an in-process
    app) or a ``base_url`` and let one be created::

        async with LoomClient(base_url="http://localhost:8000") as loom:
            run = await loom.start("nightly_report", {"day": "monday"})
            final = await loom.wait(run["run_id"])

    Pass ``token`` for a fixed bearer token, or ``token_provider`` for one
    that can be refreshed — see :data:`TokenProvider`. Neither is required:
    an unauthenticated server needs neither, and gets exactly the requests
    this client always sent before either existed.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        http: Any | None = None,
        timeout: float = 30.0,
        token: str | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        if http is None:
            import httpx

            if not base_url:
                raise ValueError("pass either base_url or an http client")
            http = httpx.AsyncClient(base_url=base_url, timeout=timeout)
            self._owns_http = True
        else:
            self._owns_http = False
        self._http = http
        if token is not None and token_provider is not None:
            raise ValueError("pass token or token_provider, not both")
        self._token_provider: TokenProvider | None = token_provider
        if token is not None:

            async def _fixed(force_refresh: bool) -> str | None:
                del force_refresh  # a fixed token has nothing to refresh to
                return token

            self._token_provider = _fixed

    async def __aenter__(self) -> LoomClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying connection pool, if this client created it."""
        if self._owns_http:
            await self._http.aclose()

    # -- requests ------------------------------------------------------------

    async def _authorization_header(self, *, force_refresh: bool) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        token = await self._token_provider(force_refresh)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", None) or {})
        headers.update(await self._authorization_header(force_refresh=False))
        response = await self._http.request(method, path, headers=headers, **kwargs)

        # One retry, only on 401, only when a provider exists to refresh —
        # a fixed `token=` retries with the same rejected value and falls
        # through to the error below, which is correct: there is nothing
        # else to try.
        if response.status_code == 401 and self._token_provider is not None:
            refreshed = await self._authorization_header(force_refresh=True)
            if refreshed:
                headers.update(refreshed)
                response = await self._http.request(method, path, headers=headers, **kwargs)

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            # Structured error bodies (`server/app.py`'s `insufficient_scope`,
            # `server/auth.py`'s 401s) nest the human sentence under `detail`
            # or `error_description` rather than being a bare string —
            # surface that, not a dict repr.
            if isinstance(detail, dict):
                detail = detail.get("detail") or detail.get("error_description") or detail
            raise LoomClientError(
                str(detail),
                status_code=response.status_code,
                www_authenticate=response.headers.get("WWW-Authenticate"),
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """``_request`` for a route that answers with a single JSON object."""
        return _as_object(await self._request(method, path, **kwargs), path)

    async def _array(self, method: str, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        """``_request`` for a route that answers with a JSON array of objects."""
        return _as_array(await self._request(method, path, **kwargs), path)

    # -- workflows -----------------------------------------------------------

    async def workflows(self, *, published: bool = True) -> list[dict[str, Any]]:
        """Every workflow the server has registered, with input schemas.

        ``published=False`` narrows it to what the serving process can actually
        start, rather than everything in the catalog.
        """
        return await self._array(
            "GET", "/workflows", params={"published": str(published).lower()}
        )

    # -- runs ----------------------------------------------------------------

    async def start(
        self,
        workflow: str,
        input: Any = None,
        *,
        idempotency_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = False,
        env: dict[str, str] | None = None,
        credentials: dict[str, str] | Any | None = None,
    ) -> dict[str, Any]:
        """Start a run. Returns the run as the server sees it.

        Pass ``idempotency_key`` when the caller may retry the request — the
        server returns the original run rather than starting a second one.
        """
        body: dict[str, Any] = {
            "workflow": workflow,
            "input": input,
            "idempotency_key": idempotency_key,
            "tags": tags or [],
            "metadata": metadata or {},
            "wait": wait,
        }
        if env:
            body["env"] = env
        if credentials is not None:
            body["credentials"] = credentials
        return await self._object("POST", "/runs", json=body)

    async def get(self, run_id: str) -> dict[str, Any]:
        return await self._object("GET", f"/runs/{run_id}")

    async def list_runs(
        self,
        *,
        workflow: str | None = None,
        status: ExecutionStatus | str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if workflow:
            params["workflow"] = workflow
        if status:
            params["status"] = status.value if isinstance(status, ExecutionStatus) else status
        return await self._array("GET", "/runs", params=params)

    async def journal(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        """Durable operations recorded for a run, in order.

        *offset* skips entries the caller has already seen. A server that
        predates the parameter ignores it and returns the whole journal, which
        the facade above re-slices.
        """
        params = {"offset": offset} if offset else None
        return await self._array("GET", f"/runs/{run_id}/journal", params=params)

    async def versions(self, workflow: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """A workflow's committed version chain, newest first."""
        return await self._array(
            "GET", f"/workflows/{workflow}/versions", params={"limit": limit}
        )

    async def activate_version(self, workflow: str, version: int) -> dict[str, Any]:
        """Make *version* the served one."""
        return await self._object(
            "POST", f"/workflows/{workflow}/versions/{version}/activate"
        )

    async def version_source(self, workflow: str, version: int) -> str:
        """The source a version was committed from."""
        payload = await self._object(
            "GET", f"/workflows/{workflow}/versions/{version}/source"
        )
        return str(payload.get("source", ""))

    async def trace(self, run_id: str) -> dict[str, Any]:
        """The workflow graph with this run overlaid on it."""
        return await self._object("GET", f"/runs/{run_id}/trace")

    async def graph(self, workflow: str) -> dict[str, Any]:
        """A workflow's structure, as React Flow nodes and edges."""
        return await self._object("GET", f"/workflows/{workflow}/graph")

    async def reports(self, run_id: str, *, offset: int = 0) -> list[dict[str, Any]]:
        """Progress a run has reported, from *offset* onward."""
        return await self._array(
            "GET", f"/runs/{run_id}/reports", params={"offset": offset}
        )

    async def send_event(self, run_id: str, name: str, payload: Any = None) -> dict[str, Any]:
        """Deliver an event, resuming the run if it was parked on it."""
        return await self._object(
            "POST", f"/runs/{run_id}/events", json={"name": name, "payload": payload}
        )

    async def approve(
        self, run_id: str, subject: str, *, approved: bool = True
    ) -> dict[str, Any]:
        """Resolve a pending ``ctx.wait_for_approval`` from outside Python."""
        return await self.send_event(run_id, f"approval:{subject}", {"approved": approved})

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self._object("POST", f"/runs/{run_id}/cancel")

    async def retry(self, run_id: str) -> dict[str, Any]:
        """Re-run a failed execution, reusing everything that already succeeded."""
        return await self._object("POST", f"/runs/{run_id}/retry")

    async def replay(self, run_id: str) -> dict[str, Any]:
        return await self._object("POST", f"/runs/{run_id}/replay")

    async def wait(
        self,
        run_id: str,
        *,
        timeout: Duration = 60.0,
        poll_interval: Duration = 0.5,
    ) -> dict[str, Any]:
        """Poll until the run reaches a terminal state.

        Returns the last observation on timeout rather than raising — a run that
        is still going is a legitimate answer, not an error.
        """
        deadline = to_seconds(timeout)
        interval = to_seconds(poll_interval)
        waited = 0.0
        run = await self.get(run_id)
        while waited < deadline:
            if ExecutionStatus(run["status"]).is_terminal:
                return run
            await asyncio.sleep(interval)
            waited += interval
            run = await self.get(run_id)
        return run

    # -- artifacts ------------------------------------------------------------

    async def list_artifacts(self) -> list[dict[str, Any]]:
        return await self._array("GET", "/artifacts")

    async def artifact_history(self, name: str) -> list[dict[str, Any]]:
        return await self._array("GET", f"/artifacts/{name}/versions")

    async def artifact_url(
        self, name: str, version: int | None = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"expires_in": expires_in}
        if version is not None:
            params["version"] = version
        return await self._object("GET", f"/artifacts/{name}/url", params=params)

    async def read_artifact(
        self, name: str, version: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if version is not None:
            params["version"] = version
        return await self._object("GET", f"/artifacts/{name}/content", params=params)

    async def put_artifact(
        self,
        name: str,
        content_b64: str,
        *,
        mime: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._object(
            "POST",
            f"/artifacts/{name}",
            json={
                "content_b64": content_b64,
                "mime": mime,
                "metadata": metadata,
            },
        )

    async def upload_url(
        self,
        name: str,
        mime: str = "application/octet-stream",
        max_size: int | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        return await self._object(
            "POST",
            f"/artifacts/{name}/upload-url",
            json={"mime": mime, "max_size": max_size, "expires_in": expires_in},
        )

    async def confirm_upload(
        self,
        upload_id: str,
        name: str,
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._object(
            "POST",
            f"/artifacts/{name}/confirm",
            json={
                "upload_id": upload_id,
                "run_id": run_id,
                "metadata": metadata,
            },
        )

    async def read_blob(
        self, ref: str, expires: int, signature: str, method: str = "GET"
    ) -> dict[str, Any]:
        import base64

        response = await self._raw(
            "GET",
            f"/blobs/{ref}",
            params={"expires": expires, "sig": signature, "method": method},
        )
        mime = (response.headers.get("content-type") or "application/octet-stream").split(
            ";"
        )[0]
        content = response.content
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "mime": mime,
            "size": len(content),
            "ref": ref,
        }

    async def write_blob(
        self,
        ref: str,
        expires: int,
        signature: str,
        content_b64: str,
        mime: str = "application/octet-stream",
        method: str = "PUT",
    ) -> dict[str, Any]:
        import base64

        return await self._object(
            "PUT",
            f"/blobs/{ref}",
            params={"expires": expires, "sig": signature, "method": method},
            content=base64.b64decode(content_b64),
            headers={"Content-Type": mime},
        )

    async def _raw(self, method: str, path: str, **kwargs: Any) -> Any:
        """Like ``_request`` but returns the HTTP response (for binary bodies)."""
        headers = dict(kwargs.pop("headers", None) or {})
        headers.update(await self._authorization_header(force_refresh=False))
        response = await self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code == 401 and self._token_provider is not None:
            refreshed = await self._authorization_header(force_refresh=True)
            if refreshed:
                headers.update(refreshed)
                response = await self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            if isinstance(detail, dict):
                detail = detail.get("detail") or detail.get("error_description") or detail
            raise LoomClientError(
                str(detail),
                status_code=response.status_code,
                www_authenticate=response.headers.get("WWW-Authenticate"),
            )
        return response
