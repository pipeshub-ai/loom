"""``io.*`` — typed external effects.

The two that matter are here: call something over HTTP, and wait for something
to call you. Both are shapes every integration workflow needs before any
specific toolset is involved.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from loom.core.exceptions import ConfigurationError
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
    connection: str = Field(
        default="",
        description=(
            "Id of a connection whose credential to send, resolved by the "
            "Runtime's ConnectionBroker at call time. Prefer this over putting "
            "a token in `headers`: this payload is journaled, and a "
            "connection's *id* is safe to record where its credential is not."
        ),
    )
    auth_header: str = Field(
        default="Authorization",
        description="Header the resolved credential is sent in.",
    )
    auth_scheme: str = Field(
        default="Bearer",
        description=(
            "Prefix before the token, e.g. 'Bearer'. Set it to '' for an API "
            "that wants the raw value — several reject a prefixed token with a "
            "401 and no explanation."
        ),
    )
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
        open_world=True,
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
        # The one node whose class is a property of the call, not the node: it
        # is what a generated workflow reaches for when no toolset covers the
        # API, and `method="DELETE"` is not a write. An unlisted method keeps
        # WRITE rather than falling to a read.
        effect_by={
            "method": {
                "GET": EffectClass.READ,
                "HEAD": EffectClass.READ,
                "OPTIONS": EffectClass.READ,
                "DELETE": EffectClass.DESTRUCTIVE,
            }
        },
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
        headers = await _with_credential(ctx, payload)

        async def request() -> HttpOut:
            import httpx

            async with httpx.AsyncClient(timeout=to_seconds(payload.timeout)) as client:
                response = await client.request(
                    payload.method.upper(),
                    payload.url,
                    headers=headers or None,
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


async def _with_credential(ctx: NodeContext, payload: HttpIn) -> dict[str, str]:
    """The request headers, with a named credential resolved into them.

    Resolved **here**, outside the journaled call, so the token never becomes
    part of what a replay serves back — the recorded payload holds the
    connection's id, and the id is what a person reading a trace needs.

    The field is ``connection`` rather than ``credential`` for a reason worth
    keeping: it names a connection, not a secret, and a field called
    ``credential`` is redacted out of the journal by the name denylist — which
    would erase the one part of this that is safe and useful to record.

    Not declared in ``NodeSpec.requires``: requirements are checked before
    every call to this node, and most calls name no credential. A node that
    demanded a broker to fetch a public URL would be worse than one that
    explains itself when a credential is actually asked for.
    """
    headers = dict(payload.headers)
    if not payload.connection:
        return headers

    try:
        broker = ctx.capability("connections")
    except ConfigurationError as exc:
        # The shared message tells a node author to declare a requirement,
        # which is advice this node deliberately does not follow. Say what the
        # *caller* has to do instead.
        raise ConfigurationError(
            f"io.http_request was asked for connection {payload.connection!r}, "
            "but this Runtime has no ConnectionBroker. Pass "
            "Runtime(connections=ConnectionBroker()) — or drop `connection` and "
            "call an endpoint that needs no credential."
        ) from exc
    resolved = await broker.resolve(payload.connection)
    scheme = payload.auth_scheme.strip()
    headers[payload.auth_header] = (
        f"{scheme} {resolved.token}" if scheme else resolved.token
    )
    return headers


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
        open_world=True,
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
