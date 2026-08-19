"""``io.http_request`` can name a credential instead of carrying one.

The node took a raw ``headers`` dict, so the only way to call a service LOOM
has no toolset for was to put a live token in the node's payload — and a node's
payload is journaled. Naming a connection is safe to record; holding its
credential is not, and that difference is what these tests pin.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from loom import Context, Runtime, workflow
from loom.core.exceptions import ConfigurationError
from loom.stores.memory import MemoryStore
from loom.toolsets.connections import ConnectionBroker, Credential

TOKEN = "sk-BROKER-SECRET"


class Sent:
    """Records the headers one request went out with."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    """Replace httpx with a client that records and answers 200."""
    import httpx

    record = Sent()

    class FakeResponse:
        status_code = 200
        text = "{}"
        headers: ClassVar[dict[str, str]] = {}
        is_success = True

        def json(self) -> dict[str, Any]:
            return {"ok": True}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def request(
            self, method: str, url: str, headers: Any = None, **kwargs: Any
        ) -> FakeResponse:
            record.headers = dict(headers or {})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return record


class StaticResolver:
    """A resolver with one credential, so no environment is involved.

    Implements the whole ``CredentialResolver`` protocol rather than only the
    method under test — a partial stub type-checks as "not a resolver", and a
    test double that does not satisfy the seam is not exercising the seam.
    """

    async def resolve(
        self, connection_id: str, scopes: list[str] | None = None
    ) -> Credential:
        if not self.has_connection(connection_id):
            raise KeyError(connection_id)
        return Credential(token=TOKEN)

    async def refresh(self, connection_id: str) -> Credential:
        return await self.resolve(connection_id)

    def has_connection(self, connection_id: str) -> bool:
        return connection_id == "acme"


def broker() -> ConnectionBroker:
    return ConnectionBroker(resolver=StaticResolver())


def calling(payload: dict[str, Any]) -> Any:
    @workflow(name=f"http_{abs(hash(str(payload))) % 100000}")
    async def flow(ctx: Context, _: None = None) -> int:
        out = await ctx.node("io.http_request", payload)
        return int(out.status)

    return flow


class TestCredentialInjection:
    @pytest.mark.asyncio()
    async def test_a_named_connection_is_sent_as_a_bearer(self, sent: Sent) -> None:
        runtime = Runtime(store=MemoryStore(), connections=broker())
        result = await runtime.run(
            calling({"url": "https://api.acme.test/v1", "connection": "acme"})
        )
        assert result.output == 200
        assert sent.headers["Authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio()
    async def test_an_empty_scheme_sends_the_raw_token(self, sent: Sent) -> None:
        """Several APIs reject a prefixed token with a 401 and no explanation.

        ClickUp is the shipped example: a personal token goes in the header
        raw, and ``Bearer pk_…`` fails with no hint why.
        """
        runtime = Runtime(store=MemoryStore(), connections=broker())
        await runtime.run(
            calling(
                {
                    "url": "https://api.acme.test/v1",
                    "connection": "acme",
                    "auth_header": "X-Api-Key",
                    "auth_scheme": "",
                }
            )
        )
        assert sent.headers == {"X-Api-Key": TOKEN}

    @pytest.mark.asyncio()
    async def test_caller_headers_survive_alongside_it(self, sent: Sent) -> None:
        runtime = Runtime(store=MemoryStore(), connections=broker())
        await runtime.run(
            calling(
                {
                    "url": "https://api.acme.test/v1",
                    "connection": "acme",
                    "headers": {"Accept": "application/json"},
                }
            )
        )
        assert sent.headers["Accept"] == "application/json"
        assert sent.headers["Authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio()
    async def test_no_connection_named_needs_no_broker(self, sent: Sent) -> None:
        """The common case is a public URL, and it must stay free."""
        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(calling({"url": "https://example.test/open"}))
        assert result.output == 200
        assert "Authorization" not in sent.headers


class TestTheTokenIsNotRecorded:
    @pytest.mark.asyncio()
    async def test_the_journal_holds_the_id_and_not_the_credential(
        self, sent: Sent
    ) -> None:
        """The whole point: the id is useful to a reader, the token is not.

        Resolution happens outside the journaled call, so the token is never
        part of what a replay serves back either.
        """
        runtime = Runtime(store=MemoryStore(), connections=broker())
        result = await runtime.run(
            calling({"url": "https://api.acme.test/v1", "connection": "acme"})
        )

        entries = await runtime.store.load_journal(result.run_id)
        recorded = str([(e.name, e.input, e.output) for e in entries])
        assert TOKEN not in recorded
        assert "'connection': 'acme'" in recorded


class TestMissingBroker:
    @pytest.mark.asyncio()
    async def test_it_says_what_the_caller_must_do(self, sent: Sent) -> None:
        """The shared capability error tells a *node author* to declare a
        requirement, which is advice this node deliberately does not follow."""
        runtime = Runtime(store=MemoryStore())
        result = await runtime.run(
            calling({"url": "https://api.acme.test/v1", "connection": "acme"})
        )

        assert result.status.value == "failed"
        assert result.error is not None
        assert "Runtime(connections=ConnectionBroker())" in result.error.message
        assert "NodeSpec.requires" not in result.error.message

    def test_the_node_does_not_declare_the_requirement(self) -> None:
        """Declaring it would refuse every call that names no connection."""
        from loom.nodes.io import HttpRequestNode

        assert "connections" not in HttpRequestNode.spec.requires


class TestCapabilityWiring:
    def test_a_runtime_exposes_connections(self) -> None:
        broker_instance = broker()
        runtime = Runtime(store=MemoryStore(), connections=broker_instance)
        assert runtime.connections is broker_instance

    def test_it_defaults_to_none(self) -> None:
        """Nothing is enforced or configured unless a host composes it in."""
        assert Runtime(store=MemoryStore()).connections is None

    @pytest.mark.asyncio()
    async def test_an_unknown_connection_surfaces_the_resolver_error(
        self, sent: Sent
    ) -> None:
        runtime = Runtime(store=MemoryStore(), connections=broker())
        result = await runtime.run(
            calling({"url": "https://api.acme.test/v1", "connection": "nope"})
        )
        assert result.status.value == "failed"
        assert not isinstance(result.error, ConfigurationError)
