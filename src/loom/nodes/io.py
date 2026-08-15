"""``io.*`` — typed external effects.

The two that matter are here: call something over HTTP, and wait for something
to call you. Both are shapes every integration workflow needs before any
specific toolset is involved.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from loom.core.retry import Retry
from loom.core.types import to_seconds
from loom.nodes.base import Node, NodeContext
from loom.nodes.registry import register_node
from loom.nodes.spec import (
    EffectClass,
    NodeCategory,
    NodeDuration,
    NodeExample,
    NodeSpec,
)

__all__ = ["HttpRequestNode", "WaitForWebhookNode"]


class HttpIn(BaseModel):
    url: str = Field(description="Absolute URL to call.")
    method: str = Field(default="GET", description="GET | POST | PUT | PATCH | DELETE")
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict, description="Query string.")
    json_body: Any = Field(default=None, alias="json", description="JSON request body.")
    timeout: NodeDuration = Field(default=30.0)
    max_attempts: int = Field(
        default=1,
        description=(
            "Retries. Leave at 1 for non-idempotent methods — a timeout after "
            "the server acted is indistinguishable from a failure, and a retry "
            "repeats the effect."
        ),
    )
    model_config = {"populate_by_name": True}


class HttpOut(BaseModel):
    status: int = 0
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = Field(default=None, alias="json")
    text: str = ""
    ok: bool = False
    model_config = {"populate_by_name": True}


@register_node
class HttpRequestNode(Node[HttpIn, HttpOut]):
    """Make an HTTP request, journaled so a replay does not repeat it."""

    spec = NodeSpec(
        id="io.http_request",
        category=NodeCategory.IO,
        import_module="loom.nodes.io",
        summary="Make an HTTP request; the response is journaled and replayed.",
        description=(
            "A 4xx or 5xx comes back as a result with ok=False rather than an "
            "exception, so a workflow can branch on it. Retries default to off "
            "because the safe number depends on the method, which only the "
            "caller knows."
        ),
        effect=EffectClass.WRITE,
        deterministic=False,
        tags=["http", "request", "api", "rest", "fetch"],
        examples=[
            NodeExample(
                payload={
                    "url": "https://api.example.com/orders",
                    "method": "POST",
                    "json": {"sku": "A-1"},
                }
            )
        ],
    )
    Input, Output = HttpIn, HttpOut

    async def run(self, ctx: NodeContext, payload: HttpIn) -> HttpOut:
        async def request() -> HttpOut:
            import httpx

            async with httpx.AsyncClient(timeout=to_seconds(payload.timeout)) as client:
                response = await client.request(
                    payload.method.upper(),
                    payload.url,
                    headers=payload.headers or None,
                    params=payload.params or None,
                    json=payload.json_body,
                )
            parsed: Any = None
            try:
                parsed = response.json()
            except Exception:
                parsed = None
            return HttpOut(
                status=response.status_code,
                headers=dict(response.headers),
                json=parsed,
                text=response.text[:100_000],
                ok=response.is_success,
            )

        response: HttpOut = await ctx.call(
            f"http:{payload.method.upper()}",
            request,
            retry=Retry(max_attempts=max(1, payload.max_attempts)),
        )
        return response


# ---------------------------------------------------------------------------


class WebhookWaitIn(BaseModel):
    event: str = Field(description="Event name this run parks on.")
    timeout: NodeDuration | None = Field(
        default=None, description="Seconds or a timedelta. None waits forever."
    )
    default: Any = Field(default=None, description="Returned if nothing arrives in time.")


class WebhookWaitOut(BaseModel):
    payload: Any = None
    timed_out: bool = False


@register_node
class WaitForWebhookNode(Node[WebhookWaitIn, WebhookWaitOut]):
    """Park until a named event is delivered to this run."""

    spec = NodeSpec(
        id="io.wait_for_webhook",
        category=NodeCategory.IO,
        import_module="loom.nodes.io",
        summary="Park until a named event is delivered to this run.",
        description=(
            "Delivered with runtime.send_event(run_id, event, payload) or "
            "POST /runs/{run}/events. The run costs nothing while parked."
        ),
        suspends=True,
        deterministic=False,
        tags=["webhook", "wait", "callback", "event"],
        examples=[
            NodeExample(payload={"event": "payment.settled", "timeout": 259200})
        ],
    )
    Input, Output = WebhookWaitIn, WebhookWaitOut

    async def run(self, ctx: NodeContext, payload: WebhookWaitIn) -> WebhookWaitOut:
        sentinel = {"__loom_timed_out__": True}
        answer = await ctx.wait_for_event(
            payload.event, timeout=payload.timeout, default=sentinel
        )
        if isinstance(answer, dict) and answer.get("__loom_timed_out__"):
            return WebhookWaitOut(payload=payload.default, timed_out=True)
        return WebhookWaitOut(payload=answer)
