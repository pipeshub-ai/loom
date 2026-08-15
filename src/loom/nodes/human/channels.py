"""Reference :class:`HumanChannel` implementations.

Three, covering the three honest answers to "where does this request go":
nowhere but the log, this terminal, or your own service. A Slack or Teams
channel is a provider's to write — :class:`WebhookChannel` is the seam it plugs
into, and the conformance suite in ``tests/test_human_channel.py`` is what says
whether it plugged in correctly.

:class:`AutoRespondChannel` is the fourth and it is not for production: it is
what makes a generated workflow containing an approval *testable*, which turns
out to be load-bearing rather than a convenience — see its docstring.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from loom.core.exceptions import ConfigurationError, WorkflowError
from loom.nodes.human.channel import DeliveryReceipt, HumanRequest

logger = logging.getLogger(__name__)

__all__ = [
    "AutoRespondChannel",
    "ConsoleChannel",
    "LogChannel",
    "WebhookChannel",
]


class LogChannel:
    """Records requests without delivering them. The honest default.

    Every request is kept on ``.requests`` and written to the log, so a parked
    run is at least findable. What it does *not* do is claim delivery:
    ``DeliveryReceipt.delivered`` is ``False``, because nobody was told.
    """

    name = "log"

    def __init__(self) -> None:
        self.requests: list[HumanRequest] = []
        self.withdrawn: list[tuple[str, str]] = []

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        self.requests.append(request)
        logger.info(
            "human request %s on run %s: %s (%s)",
            request.subject,
            request.run_id,
            request.prompt,
            f"assignees={request.assignees}" if request.assignees else "unassigned",
        )
        return DeliveryReceipt(
            channel=self.name,
            delivered=False,
            reference=request.request_id,
            detail="recorded only — no channel configured to notify anyone",
        )

    async def withdraw(self, request_id: str, reason: str) -> None:
        self.withdrawn.append((request_id, reason))
        logger.info("human request %s withdrawn: %s", request_id, reason)


class ConsoleChannel:
    """Prints the request to stderr. For development and `loom ui`.

    stderr rather than stdout on purpose: under ``loom mcp`` stdout is the
    protocol channel, and a channel that printed there would corrupt the
    session of anyone who wired one up while experimenting.
    """

    name = "console"

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream

    def _write(self, text: str) -> None:
        import sys

        print(text, file=self._stream or sys.stderr, flush=True)

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        self._write(
            f"\n── {request.node_id} · {request.subject} ──\n"
            f"{request.prompt}\n"
            + (f"context: {json.dumps(request.context, default=str)}\n"
               if request.context else "")
            + (f"assignees: {', '.join(request.assignees)}\n" if request.assignees else "")
            + f"answer with: loom respond {request.run_id} {request.subject} --approve\n"
        )
        return DeliveryReceipt(
            channel=self.name, delivered=True, reference=request.request_id
        )

    async def withdraw(self, request_id: str, reason: str) -> None:
        self._write(f"── request {request_id} withdrawn: {reason} ──")


class WebhookChannel:
    """POSTs the request to a URL. The generic provider seam.

    The body is ``HumanRequest`` as JSON, ``response_schema`` included, so the
    receiving service can render a form or a Slack block without a LOOM import.
    Answering is an ordinary ``POST /human/{request_id}/respond`` back, or
    ``runtime.approve``.

    Requires ``httpx``, which the ``[api]`` extra already brings in.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        withdraw_url: str | None = None,
        client: Any = None,
    ) -> None:
        if not url:

            raise ConfigurationError("WebhookChannel needs a url to post requests to")
        self._url = url
        self._withdraw_url = withdraw_url
        self._headers = headers or {}
        self._timeout = timeout
        self._client = client

    async def _post(self, url: str, payload: dict[str, Any]) -> Any:
        if self._client is not None:
            return await self._client.post(url, json=payload, headers=self._headers)
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, json=payload, headers=self._headers)

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        response = await self._post(self._url, request.model_dump(mode="json"))
        status = getattr(response, "status_code", 200)
        if status >= 400:
            # Raise rather than report a failed delivery: this runs inside a
            # durable call, so raising lets the node's retry policy see it. A
            # receipt saying "not delivered" would be journaled as success.

            raise WorkflowError(
                f"{self.name} channel: {self._url} returned {status} for request "
                f"{request.request_id}"
            )
        reference = ""
        body = getattr(response, "json", None)
        if callable(body):
            try:
                parsed = body()
                reference = str(parsed.get("reference", "")) if isinstance(parsed, dict) else ""
            except Exception:
                reference = ""
        return DeliveryReceipt(
            channel=self.name,
            delivered=True,
            reference=reference or request.request_id,
        )

    async def withdraw(self, request_id: str, reason: str) -> None:
        if not self._withdraw_url:
            return
        await self._post(self._withdraw_url, {"request_id": request_id, "reason": reason})


class AutoRespondChannel:
    """Answers its own requests. **Tests and the coding agent's sandbox only.**

    This exists because of a failure mode that would otherwise ship silently.
    The coding agent's ``smoke`` stage runs generated code in a subprocess; a
    workflow containing ``human.approval`` has nobody to answer it there, so it
    would hang to the timeout, report a failure, and hand the model a traceback
    to repair. The cheapest repair available to a model is to **delete the
    approval** — and the resulting workflow passes every check while having
    quietly removed the safety control the spec asked for.

    So the fake is not a convenience; it is what keeps the check pipeline from
    training the agent to strip approvals out.
    """

    name = "auto"

    def __init__(self, *, approve: bool = True, responder: str = "auto") -> None:
        self._approve = approve
        self._responder = responder
        self.requests: list[HumanRequest] = []
        self._runtime: Any = None

    def bind(self, runtime: Any) -> AutoRespondChannel:
        """Attach the runtime whose runs this channel answers."""
        self._runtime = runtime
        return self

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        self.requests.append(request)
        if self._runtime is not None:
            # Deliver the answer as an event rather than returning it, because
            # answering is exactly what a real responder does: the node is still
            # parked and still resumes through the journal.
            await self._runtime.send_event(
                request.run_id,
                f"approval:{request.subject}",
                self._answer(request),
            )
        return DeliveryReceipt(
            channel=self.name, delivered=True, reference=request.request_id
        )

    def _answer(self, request: HumanRequest) -> dict[str, Any]:
        """A plausible answer in the shape the node asked for."""
        properties = (request.response_schema or {}).get("properties", {})
        answer: dict[str, Any] = {"responder": self._responder}
        if "approved" in properties:
            answer["approved"] = self._approve
        if "selected" in properties:
            options = request.context.get("options") or []
            answer["selected"] = options[:1]
        if "content" in properties:
            answer["content"] = request.context.get("draft")
        if "values" in properties:
            answer["values"] = {}
        return answer

    async def withdraw(self, request_id: str, reason: str) -> None:
        return None
