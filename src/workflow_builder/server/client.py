"""Thin HTTP client for a LOOM server.

Mirrors the shape of :class:`Runtime` for the operations that cross a process
boundary. Ports of this to other languages are a few dozen lines each — that is
the point of having the boundary at all.
"""

from __future__ import annotations

import asyncio
from typing import Any

from workflow_builder.core.models import ExecutionStatus
from workflow_builder.core.types import Duration, to_seconds


class LoomClientError(Exception):
    """A LOOM server returned an error response."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """Whether the same request could succeed later.

        429 is flow control asking for patience; 5xx is usually transient.
        """
        return self.status_code == 429 or self.status_code >= 500


class LoomClient:
    """Async client for the LOOM HTTP API.

    Either pass a ``httpx.AsyncClient`` (handy for tests against an in-process
    app) or a ``base_url`` and let one be created::

        async with LoomClient(base_url="http://localhost:8000") as loom:
            run = await loom.start("nightly_report", {"day": "monday"})
            final = await loom.wait(run["run_id"])
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        http: Any | None = None,
        timeout: float = 30.0,
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

    async def __aenter__(self) -> LoomClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying connection pool, if this client created it."""
        if self._owns_http:
            await self._http.aclose()

    # -- requests ------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise LoomClientError(str(detail), status_code=response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- workflows -----------------------------------------------------------

    async def workflows(self) -> list[dict[str, Any]]:
        """Every workflow the server has registered, with input schemas."""
        return await self._request("GET", "/workflows")

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
    ) -> dict[str, Any]:
        """Start a run. Returns the run as the server sees it.

        Pass ``idempotency_key`` when the caller may retry the request — the
        server returns the original run rather than starting a second one.
        """
        return await self._request(
            "POST",
            "/runs",
            json={
                "workflow": workflow,
                "input": input,
                "idempotency_key": idempotency_key,
                "tags": tags or [],
                "metadata": metadata or {},
                "wait": wait,
            },
        )

    async def get(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/runs/{run_id}")

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
        return await self._request("GET", "/runs", params=params)

    async def journal(self, run_id: str) -> list[dict[str, Any]]:
        """Durable operations recorded for a run, in order."""
        return await self._request("GET", f"/runs/{run_id}/journal")

    async def send_event(self, run_id: str, name: str, payload: Any = None) -> dict[str, Any]:
        """Deliver an event, resuming the run if it was parked on it."""
        return await self._request(
            "POST", f"/runs/{run_id}/events", json={"name": name, "payload": payload}
        )

    async def approve(self, run_id: str, subject: str, *, approved: bool = True) -> Any:
        """Resolve a pending ``ctx.wait_for_approval`` from outside Python."""
        return await self.send_event(run_id, f"approval:{subject}", {"approved": approved})

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/runs/{run_id}/cancel")

    async def retry(self, run_id: str) -> dict[str, Any]:
        """Re-run a failed execution, reusing everything that already succeeded."""
        return await self._request("POST", f"/runs/{run_id}/retry")

    async def replay(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/runs/{run_id}/replay")

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
