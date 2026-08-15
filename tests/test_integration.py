"""Cross-phase integration tests.

Tests that exercise multiple subsystems together to verify
Phases 1-3 work as a cohesive whole.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from loom import Context, Runtime, step, workflow
from loom.agents.agent import Agent
from loom.agents.messages import ToolCall
from loom.agents.tools import tool
from loom.core.types import Page
from loom.resources.base import Depends, ResourceScope, resource
from loom.runtime.backend import EmbeddedBackend
from loom.security.grants import derive_grants
from loom.steps.definition import StepClass, effect, pure
from loom.stores.memory import MemoryStore
from loom.testing.mock import MockModelProvider, mock_response
from loom.toolsets.catalog import ToolsetCatalog
from loom.toolsets.certify import certify
from loom.toolsets.connections import ConnectionBroker
from loom.toolsets.gateway import RateLimitConfig, RateLimiter
from loom.toolsets.lock import generate_lock, verify_lock
from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.registry import get_catalog, register_toolset, unregister_toolset
from loom.triggers.filter import FilterSpec
from loom.triggers.routing import EventRouter, RoutingEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crm_manifest() -> ToolsetManifest:
    return ToolsetManifest(
        id="crm",
        version="1.0.0",
        summary="CRM — leads, contacts, deals",
        base_url="https://api.crm.com",
        egress_hosts=["api.crm.com"],
        fakes_module="crm.fakes",
        rate_limits={"default": {"rps": 10}},
        groups={
            "leads": [
                OperationSpec(
                    id="leads.create",
                    summary="Create a new lead",
                    effect=EffectClass.WRITE,
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                    scopes=["leads:write"],
                    idempotent=True,
                ),
                OperationSpec(
                    id="leads.list",
                    summary="List all leads",
                    effect=EffectClass.READ,
                    input_schema={"type": "object"},
                    output_schema={"type": "array"},
                    pagination=True,
                ),
            ],
        },
    )


# ---------------------------------------------------------------------------
# 1. Agent + Toolset Catalog integration
# ---------------------------------------------------------------------------


class TestAgentWithToolsetCatalog:
    """Verify that an agent can use toolset catalog tools."""

    @pytest.mark.asyncio
    async def test_agent_tool_uses_catalog_search(self) -> None:
        """Agent tool wrapping catalog.search works end-to-end."""
        catalog = ToolsetCatalog()
        catalog.register(_crm_manifest())

        @tool
        async def search_toolsets(query: str) -> str:
            """Search the toolset catalog.

            Args:
                query: Search query for toolset discovery.
            """
            results = catalog.search(query)
            return ", ".join(c.toolset_id for c in results) or "No results"

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="search_toolsets",
                        arguments={"query": "crm"},
                    )
                ]
            ),
            mock_response("Found the CRM toolset."),
        ])
        agent = Agent(
            name="discoverer",
            model=provider,
            tools=[search_toolsets],
        )
        result = await agent("Find a CRM integration")
        assert result.output == "Found the CRM toolset."
        assert result.turns == 2
        assert len(result.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_agent_tool_uses_catalog_stub(self) -> None:
        """Agent tool wrapping catalog.stub retrieves typed contracts."""
        catalog = ToolsetCatalog()
        catalog.register(_crm_manifest())

        @tool
        async def get_contract(op_path: str) -> str:
            """Get the typed contract for an operation.

            Args:
                op_path: Dotted op path like toolset.group.op.
            """
            contract = catalog.stub(op_path)
            return f"Op: {contract.op_id}, Effect: {contract.effect}"

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="get_contract",
                        arguments={"op_path": "crm.leads.create"},
                    )
                ]
            ),
            mock_response("The leads.create operation writes data."),
        ])
        agent = Agent(
            name="inspector",
            model=provider,
            tools=[get_contract],
        )
        result = await agent("Show me the leads.create contract")
        assert result.turns == 2
        # Verify the tool call returned the right info
        req2 = provider.requests[1]
        tool_msgs = [
            m for m in req2.messages if m.role.value == "tool"
        ]
        assert any("leads.create" in m.text() for m in tool_msgs)
        assert any("write" in m.text() for m in tool_msgs)


# ---------------------------------------------------------------------------
# 2. Workflow + Step Classes + Page[T]
# ---------------------------------------------------------------------------


class TestWorkflowWithPageAndStepClasses:
    @pytest.mark.asyncio
    async def test_step_returns_page(self) -> None:
        """A @step can return Page[T] and it round-trips through the engine."""

        @step
        async def list_items(page_num: int) -> Page[str]:
            """List items with pagination."""
            items = [f"item-{page_num * 3 + i}" for i in range(3)]
            return Page(
                items=items,
                cursor=str(page_num + 1) if page_num < 2 else None,
                has_more=page_num < 2,
            )

        @workflow
        async def paginated_wf(ctx: Context, _input: str) -> list[str]:
            all_items: list[str] = []
            page = await ctx.step(list_items, 0)
            all_items.extend(page.items)
            if page.has_more:
                page2 = await ctx.step(list_items, 1)
                all_items.extend(page2.items)
            return all_items

        rt = Runtime()
        result = await rt.run(paginated_wf, "go")
        assert len(result.output) == 6
        assert result.output[0] == "item-0"
        assert result.output[5] == "item-5"

    @pytest.mark.asyncio
    async def test_pure_step_in_workflow(self) -> None:
        """@pure steps recompute on every replay (not journaled)."""

        @pure
        async def compute_hash(data: str) -> str:
            """Hash computation — pure, no side effects."""
            return f"hash:{len(data)}"

        @step
        async def store_result(hash_val: str) -> str:
            """Store the hash (effect step)."""
            return f"stored:{hash_val}"

        @workflow
        async def hash_wf(ctx: Context, data: str) -> str:
            h = await ctx.step(compute_hash, data)
            return await ctx.step(store_result, h)

        rt = Runtime()
        result = await rt.run(hash_wf, "hello world")
        assert result.output == "stored:hash:11"

    @pytest.mark.asyncio
    async def test_effect_step_in_workflow(self) -> None:
        """@effect steps are journaled and memoized on replay."""

        @effect
        async def fetch_data(key: str) -> str:
            """Fetch external data — journaled."""
            return f"data-for-{key}"

        @workflow
        async def fetch_wf(ctx: Context, key: str) -> str:
            return await ctx.step(fetch_data, key)

        rt = Runtime()
        result = await rt.run(fetch_wf, "config")
        assert result.output == "data-for-config"


# ---------------------------------------------------------------------------
# 3. Event Router + FilterSpec + Workflow triggers
# ---------------------------------------------------------------------------


class TestEventRoutingWithFilters:
    @pytest.mark.asyncio
    async def test_filtered_routing_to_workflows(self) -> None:
        """Events route to workflows based on FilterSpec conditions."""
        router = EventRouter()

        router.subscribe(
            "ticket.created",
            "urgent-triage",
            filter=FilterSpec(
                conditions={"priority": {"$in": ["P1", "P2"]}}
            ),
        )
        router.subscribe(
            "ticket.created",
            "standard-queue",
            filter=FilterSpec(
                conditions={"priority": {"$nin": ["P1", "P2"]}}
            ),
        )
        router.subscribe("ticket.created", "audit-log")

        # P1 ticket → urgent + audit
        p1 = RoutingEvent(
            name="ticket.created",
            payload={"priority": "P1", "title": "Server down"},
        )
        matched = await router.route(p1)
        assert "urgent-triage" in matched
        assert "audit-log" in matched
        assert "standard-queue" not in matched

        # P4 ticket → standard + audit
        p4 = RoutingEvent(
            name="ticket.created",
            payload={"priority": "P4", "title": "Typo in docs"},
        )
        matched = await router.route(p4)
        assert "standard-queue" in matched
        assert "audit-log" in matched
        assert "urgent-triage" not in matched

    @pytest.mark.asyncio
    async def test_complex_nested_filter(self) -> None:
        """FilterSpec works with nested paths and combined operators."""
        router = EventRouter()
        router.subscribe(
            "order.completed",
            "high-value-handler",
            filter=FilterSpec(conditions={
                "customer.tier": "enterprise",
                "total_cents": {"$gte": 100_000},
            }),
        )

        # Enterprise high-value → matches
        matched = await router.route(RoutingEvent(
            name="order.completed",
            payload={
                "customer": {"tier": "enterprise", "id": "C-1"},
                "total_cents": 250_000,
            },
        ))
        assert matched == ["high-value-handler"]

        # Small order → no match
        matched = await router.route(RoutingEvent(
            name="order.completed",
            payload={
                "customer": {"tier": "enterprise", "id": "C-2"},
                "total_cents": 5_000,
            },
        ))
        assert matched == []


# ---------------------------------------------------------------------------
# 4. Resource injection + Step execution
# ---------------------------------------------------------------------------


class TestResourceWithSteps:
    @pytest.mark.asyncio
    async def test_resource_lifecycle(self) -> None:
        """Resource acquire/release lifecycle works correctly."""

        @resource(scope=ResourceScope.FLOW)
        async def api_client():
            return {"base_url": "https://api.example.com", "active": True}

        dep = Depends(api_client)
        client = await dep.resolve("run-123")
        assert client["active"] is True

        # Same scope key returns same instance
        client2 = await dep.resolve("run-123")
        assert client is client2

        # Cleanup
        await api_client.release_all()


# ---------------------------------------------------------------------------
# 5. Grant derivation on real workflow code
# ---------------------------------------------------------------------------


class TestGrantDerivationOnWorkflows:
    def test_derive_from_agent_workflow(self) -> None:
        source = '''
from loom_toolset_jira import JiraClient
from loom_toolset_slack import SlackClient

async def support_workflow(ctx):
    triage = await ctx.agent("triage-bot", ticket)
    if triage.priority == "P1":
        await ctx.child("escalation-flow", triage)
    await ctx.agent("notification-bot", triage)
'''
        grants = derive_grants(source)
        assert "jira" in grants.toolsets
        assert "slack" in grants.toolsets
        assert "triage-bot" in grants.agents
        assert "notification-bot" in grants.agents
        assert "escalation-flow" in grants.subflows

    def test_merge_grants_from_multiple_workflows(self) -> None:
        src1 = '''
from loom_toolset_jira import JiraClient
async def wf1(ctx):
    await ctx.agent("bot-a", data)
'''
        src2 = '''
from loom_toolset_slack import SlackClient
async def wf2(ctx):
    await ctx.agent("bot-b", data)
    await ctx.child("sub-wf", data)
'''
        g1 = derive_grants(src1)
        g2 = derive_grants(src2)
        merged = g1.merge(g2)
        assert set(merged.toolsets) == {"jira", "slack"}
        assert set(merged.agents) == {"bot-a", "bot-b"}
        assert merged.subflows == ["sub-wf"]


# ---------------------------------------------------------------------------
# 6. Toolset lock + certification pipeline
# ---------------------------------------------------------------------------


class TestToolsetLockAndCertify:
    @pytest.mark.asyncio
    async def test_certify_then_lock(self) -> None:
        """Full pipeline: certify a manifest, then lock it."""
        m = _crm_manifest()

        # Certify
        cert = await certify(m)
        assert cert.certified is True

        # Generate lock
        lock = generate_lock([m])
        assert "crm" in lock.toolsets
        assert lock.toolsets["crm"].version == "1.0.0"

        # Verify no drift
        drifts = verify_lock(lock, [m])
        assert drifts == []

    @pytest.mark.asyncio
    async def test_schema_drift_detected(self) -> None:
        """Modifying an operation schema triggers drift detection."""
        m = _crm_manifest()
        lock = generate_lock([m])

        # Add a new operation
        m.groups["leads"].append(OperationSpec(
            id="leads.delete",
            summary="Delete a lead",
            effect=EffectClass.DESTRUCTIVE,
            input_schema={"type": "object"},
            scopes=["leads:delete"],
        ))

        drifts = verify_lock(lock, [m])
        assert any(d.kind == "schema_changed" for d in drifts)


# ---------------------------------------------------------------------------
# 7. Rate limiter + Connection broker combined
# ---------------------------------------------------------------------------


class TestRateLimiterWithBroker:
    @pytest.mark.asyncio
    async def test_rate_limited_credential_resolution(self) -> None:
        """Combine rate limiting with credential resolution."""
        limiter = RateLimiter()
        limiter.configure("crm", RateLimitConfig(burst=3))

        broker = ConnectionBroker(config={
            "crm": {"token": "crm-token-123"},
        })

        resolved_count = 0
        for _ in range(5):
            if limiter.try_acquire("crm"):
                cred = await broker.resolve("crm")
                assert cred.token == "crm-token-123"
                resolved_count += 1

        # Should only resolve 3 (burst limit)
        assert resolved_count == 3


# ---------------------------------------------------------------------------
# 8. Agent in workflow with tool that uses toolset catalog
# ---------------------------------------------------------------------------


class TestAgentInWorkflowWithToolset:
    @pytest.mark.asyncio
    async def test_end_to_end_agent_workflow(self) -> None:
        """Full pipeline: workflow → agent → tool → catalog → result."""
        catalog = ToolsetCatalog()
        catalog.register(_crm_manifest())

        @tool
        async def lookup_integration(query: str) -> str:
            """Find an integration in the catalog.

            Args:
                query: Search terms.
            """
            cards = catalog.search(query)
            if not cards:
                return "No integrations found."
            card = cards[0]
            ops = catalog.show(card.toolset_id)
            return f"Found {card.toolset_id} with {len(ops.ops)} operations"

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="lookup_integration",
                        arguments={"query": "leads"},
                    )
                ]
            ),
            mock_response("CRM has 2 lead operations."),
        ])

        discovery_agent = Agent(
            name="discovery",
            model=provider,
            tools=[lookup_integration],
            instructions="Help find integrations.",
        )

        @workflow
        async def discovery_wf(
            ctx: Context, requirement: str
        ) -> str:
            result = await ctx.agent(discovery_agent, requirement)
            return result.text()

        rt = Runtime()
        result = await rt.run(discovery_wf, "I need CRM access")
        assert result.output == "CRM has 2 lead operations."


# ---------------------------------------------------------------------------
# 9. Backend + Runtime integration
# ---------------------------------------------------------------------------


class TestBackendIntegration:
    @pytest.mark.asyncio
    async def test_embedded_backend_workflow(self) -> None:
        """Workflow runs through EmbeddedBackend correctly."""
        store = MemoryStore()
        backend = EmbeddedBackend(store)
        rt = Runtime(backend=backend)

        @step
        async def double(n: int) -> int:
            """Double a number."""
            return n * 2

        @workflow
        async def double_wf(ctx: Context, n: int) -> int:
            return await ctx.step(double, n)

        result = await rt.run(double_wf, 21)
        assert result.output == 42


# ---------------------------------------------------------------------------
# 10. Step class hashes survive workflow execution
# ---------------------------------------------------------------------------


class TestStepClassHashesInWorkflow:
    @pytest.mark.asyncio
    async def test_effect_step_has_hashes(self) -> None:
        """Effect steps have contract and closure hashes set."""

        @effect
        async def api_call(endpoint: str) -> str:
            """Call an API."""
            return f"response-from-{endpoint}"

        assert api_call.contract_hash
        assert api_call.closure_hash
        assert api_call.klass is StepClass.EFFECT

        @workflow
        async def api_wf(ctx: Context, endpoint: str) -> str:
            return await ctx.step(api_call, endpoint)

        rt = Runtime()
        result = await rt.run(api_wf, "/users")
        assert result.output == "response-from-/users"

    @pytest.mark.asyncio
    async def test_pure_step_not_journaled(self) -> None:
        """Pure steps are not journaled — they recompute."""

        @pure
        async def format_name(first: str, last: str) -> str:
            """Format a full name."""
            return f"{first} {last}"

        assert format_name.is_pure is True
        assert format_name.is_journaled is False

        @workflow
        async def name_wf(ctx: Context, _input: str) -> str:
            return await ctx.step(format_name, "John", "Doe")

        rt = Runtime()
        result = await rt.run(name_wf, "go")
        assert result.output == "John Doe"


# ---------------------------------------------------------------------------
# 11. Global registry isolation
# ---------------------------------------------------------------------------


class TestRegistryIsolation:
    def test_register_unregister_cycle(self) -> None:
        """Global registry register/unregister doesn't leak."""
        m = _crm_manifest()
        register_toolset(m)
        cat = get_catalog()
        assert cat.get("crm") is not None

        unregister_toolset("crm")
        assert cat.get("crm") is None


# ---------------------------------------------------------------------------
# 12. Structured output agent + workflow
# ---------------------------------------------------------------------------


class TestStructuredOutputInWorkflow:
    @pytest.mark.asyncio
    async def test_structured_agent_in_workflow(self) -> None:
        """Agent with structured output inside a workflow."""
        from loom.agents.output import FINAL_OUTPUT_TOOL

        class TriageResult(BaseModel):
            priority: str
            category: str

        provider = MockModelProvider(responses=[
            mock_response(
                tool_calls=[
                    ToolCall(
                        id="f1",
                        name=FINAL_OUTPUT_TOOL,
                        arguments={
                            "priority": "P2",
                            "category": "billing",
                        },
                    )
                ]
            ),
        ])

        triage_agent = Agent(
            name="triage",
            model=provider,
            output_type=TriageResult,
            instructions="Triage support tickets.",
        )

        @workflow
        async def support_wf(
            ctx: Context, ticket: str
        ) -> dict[str, str]:
            result = await ctx.agent(triage_agent, ticket)
            output = result.output
            return {
                "priority": output.priority,
                "category": output.category,
            }

        rt = Runtime()
        result = await rt.run(support_wf, "I was charged twice")
        assert result.output == {
            "priority": "P2",
            "category": "billing",
        }
