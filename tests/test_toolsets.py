"""Tests for the toolset subsystem — manifest, catalog, registry, connections,
rate limiting, lock/drift, and certification."""

from __future__ import annotations

import pytest

from workflow_builder.toolsets.catalog import (
    IndexCard,
    OpContract,
    OpTable,
    ToolsetCatalog,
)
from workflow_builder.toolsets.certify import (
    CertificationResult,
    certify,
)
from workflow_builder.toolsets.connections import ConnectionBroker, Credential
from workflow_builder.toolsets.gateway import RateLimitConfig, RateLimiter
from workflow_builder.toolsets.lock import (
    ToolsetLock,
    generate_lock,
    verify_lock,
)
from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from workflow_builder.toolsets.registry import (
    get_catalog,
    register_toolset,
    unregister_toolset,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _salesforce_manifest() -> ToolsetManifest:
    return ToolsetManifest(
        id="salesforce",
        version="1.0.0",
        summary="Salesforce CRM — leads, contacts, opportunities",
        description="Full Salesforce REST API integration",
        base_url="https://api.salesforce.com",
        egress_hosts=["api.salesforce.com"],
        fakes_module="loom_toolset_salesforce.fakes",
        rate_limits={"default": {"rps": 10}},
        groups={
            "leads": [
                OperationSpec(
                    id="leads.upsert",
                    summary="Create or update a lead",
                    effect=EffectClass.WRITE,
                    input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
                    output_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                    scopes=["leads:write"],
                    idempotent=True,
                ),
                OperationSpec(
                    id="leads.search",
                    summary="Search leads by criteria",
                    effect=EffectClass.READ,
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                    output_schema={"type": "array"},
                    pagination=True,
                ),
            ],
            "contacts": [
                OperationSpec(
                    id="contacts.get",
                    summary="Get a contact by ID",
                    effect=EffectClass.READ,
                    input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                    output_schema={"type": "object"},
                ),
            ],
        },
    )


def _slack_manifest() -> ToolsetManifest:
    return ToolsetManifest(
        id="slack",
        version="2.1.0",
        summary="Slack messaging — channels, messages, users",
        base_url="https://slack.com/api",
        egress_hosts=["slack.com"],
        fakes_module="loom_toolset_slack.fakes",
        rate_limits={"default": {"rps": 5}},
        groups={
            "chat": [
                OperationSpec(
                    id="chat.post_message",
                    summary="Post a message to a channel",
                    effect=EffectClass.WRITE,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "channel": {"type": "string"},
                            "text": {"type": "string"},
                        },
                    },
                    output_schema={"type": "object"},
                    scopes=["chat:write"],
                ),
            ],
        },
    )


# ---------------------------------------------------------------------------
# ToolsetManifest
# ---------------------------------------------------------------------------


class TestToolsetManifest:
    def test_all_operations(self) -> None:
        m = _salesforce_manifest()
        ops = m.all_operations()
        assert len(ops) == 3
        assert {op.id for op in ops} == {"leads.upsert", "leads.search", "contacts.get"}

    def test_find_operation(self) -> None:
        m = _salesforce_manifest()
        op = m.find_operation("leads.upsert")
        assert op is not None
        assert op.effect is EffectClass.WRITE

    def test_find_operation_missing(self) -> None:
        m = _salesforce_manifest()
        assert m.find_operation("nonexistent") is None

    def test_effect_class_values(self) -> None:
        assert EffectClass.READ == "read"
        assert EffectClass.WRITE == "write"
        assert EffectClass.DESTRUCTIVE == "destructive"


# ---------------------------------------------------------------------------
# ToolsetCatalog — Three-Tier Disclosure
# ---------------------------------------------------------------------------


class TestToolsetCatalog:
    def test_register_and_list(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        cat.register(_slack_manifest())
        assert set(cat.toolset_ids) == {"salesforce", "slack"}

    def test_search_by_keyword(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        cat.register(_slack_manifest())
        results = cat.search("crm")
        assert len(results) == 1
        assert results[0].toolset_id == "salesforce"

    def test_search_multi_term(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        cat.register(_slack_manifest())
        results = cat.search("message channel")
        assert len(results) >= 1
        assert results[0].toolset_id == "slack"

    def test_search_no_match(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        assert cat.search("nonexistent") == []

    def test_search_limit(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        cat.register(_slack_manifest())
        results = cat.search("a", limit=1)
        assert len(results) == 1

    def test_search_returns_index_cards(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        results = cat.search("salesforce")
        assert len(results) == 1
        card = results[0]
        assert isinstance(card, IndexCard)
        assert card.toolset_id == "salesforce"
        assert "leads" in card.groups

    def test_show_all_groups(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        table = cat.show("salesforce")
        assert isinstance(table, OpTable)
        assert len(table.ops) == 3

    def test_show_single_group(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        table = cat.show("salesforce", group="leads")
        assert len(table.ops) == 2
        assert all(op.id.startswith("leads.") for op in table.ops)

    def test_show_missing_toolset(self) -> None:
        cat = ToolsetCatalog()
        with pytest.raises(KeyError, match="not found"):
            cat.show("nonexistent")

    def test_stub_returns_contract(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        contract = cat.stub("salesforce.leads.upsert")
        assert isinstance(contract, OpContract)
        assert contract.op_id == "leads.upsert"
        assert contract.effect is EffectClass.WRITE
        assert contract.idempotent is True
        assert "name" in contract.input_schema.get("properties", {})

    def test_stub_missing_toolset(self) -> None:
        cat = ToolsetCatalog()
        with pytest.raises(KeyError, match="not found"):
            cat.stub("nonexistent.op")

    def test_stub_missing_op(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        with pytest.raises(KeyError, match="not found"):
            cat.stub("salesforce.nonexistent.op")

    def test_stub_invalid_path(self) -> None:
        cat = ToolsetCatalog()
        with pytest.raises(ValueError, match="Invalid op path"):
            cat.stub("noDotPath")

    def test_unregister(self) -> None:
        cat = ToolsetCatalog()
        cat.register(_salesforce_manifest())
        cat.unregister("salesforce")
        assert cat.toolset_ids == []


# ---------------------------------------------------------------------------
# Registry (module-level catalog)
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_get(self) -> None:
        m = _salesforce_manifest()
        register_toolset(m)
        cat = get_catalog()
        assert cat.get("salesforce") is not None
        unregister_toolset("salesforce")


# ---------------------------------------------------------------------------
# ConnectionBroker
# ---------------------------------------------------------------------------


class TestConnectionBroker:
    @pytest.mark.asyncio
    async def test_resolve_from_config(self) -> None:
        broker = ConnectionBroker(config={
            "jira": {"token": "abc123", "scopes": ["issues:read"]},
        })
        cred = await broker.resolve("jira")
        assert cred.token == "abc123"
        assert cred.scopes == ["issues:read"]

    @pytest.mark.asyncio
    async def test_resolve_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_CONN_GITHUB_TOKEN", "ghp_test")
        broker = ConnectionBroker()
        cred = await broker.resolve("github")
        assert cred.token == "ghp_test"

    @pytest.mark.asyncio
    async def test_resolve_missing(self) -> None:
        broker = ConnectionBroker()
        with pytest.raises(KeyError, match="No credential found"):
            await broker.resolve("nonexistent")

    def test_has_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_CONN_SLACK_TOKEN", "xoxb-test")
        broker = ConnectionBroker()
        assert broker.has_connection("slack") is True
        assert broker.has_connection("missing") is False

    @pytest.mark.asyncio
    async def test_scoped_resolution(self) -> None:
        broker = ConnectionBroker(config={
            "jira": {"token": "abc123"},
        })
        cred = await broker.resolve("jira", scopes=["issues:write"])
        assert cred.scopes == ["issues:write"]

    def test_credential_not_expired(self) -> None:
        cred = Credential(token="test")
        assert cred.expired is False


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_try_acquire_succeeds(self) -> None:
        limiter = RateLimiter()
        limiter.configure("test", RateLimitConfig(requests_per_second=10, burst=5))
        assert limiter.try_acquire("test") is True

    def test_burst_exhaustion(self) -> None:
        limiter = RateLimiter()
        limiter.configure("test", RateLimitConfig(requests_per_second=10, burst=3))
        assert limiter.try_acquire("test") is True
        assert limiter.try_acquire("test") is True
        assert limiter.try_acquire("test") is True
        assert limiter.try_acquire("test") is False

    def test_auto_configure(self) -> None:
        limiter = RateLimiter()
        # Auto-configures with defaults on first use
        assert limiter.try_acquire("auto") is True

    def test_reset(self) -> None:
        limiter = RateLimiter()
        limiter.configure("test", RateLimitConfig(burst=2))
        limiter.try_acquire("test")
        limiter.try_acquire("test")
        assert limiter.try_acquire("test") is False
        limiter.reset("test")
        assert limiter.try_acquire("test") is True

    @pytest.mark.asyncio
    async def test_acquire_timeout(self) -> None:
        limiter = RateLimiter()
        limiter.configure("test", RateLimitConfig(requests_per_second=0.1, burst=1))
        limiter.try_acquire("test")  # exhaust
        with pytest.raises(TimeoutError, match="Rate limit timeout"):
            await limiter.acquire("test", timeout=0.05)


# ---------------------------------------------------------------------------
# ToolsetLock & Drift Detection
# ---------------------------------------------------------------------------


class TestToolsetLock:
    def test_generate_lock(self) -> None:
        manifests = [_salesforce_manifest(), _slack_manifest()]
        lock = generate_lock(manifests)
        assert "salesforce" in lock.toolsets
        assert "slack" in lock.toolsets
        assert lock.toolsets["salesforce"].version == "1.0.0"

    def test_verify_no_drift(self) -> None:
        manifests = [_salesforce_manifest(), _slack_manifest()]
        lock = generate_lock(manifests)
        drifts = verify_lock(lock, manifests)
        assert drifts == []

    def test_verify_version_changed(self) -> None:
        manifests = [_salesforce_manifest()]
        lock = generate_lock(manifests)
        updated = _salesforce_manifest()
        updated.version = "2.0.0"
        drifts = verify_lock(lock, [updated])
        assert len(drifts) >= 1
        assert any(d.kind == "version_changed" for d in drifts)

    def test_verify_removed(self) -> None:
        manifests = [_salesforce_manifest(), _slack_manifest()]
        lock = generate_lock(manifests)
        drifts = verify_lock(lock, [_salesforce_manifest()])
        assert any(d.kind == "removed" and d.toolset_id == "slack" for d in drifts)

    def test_verify_added(self) -> None:
        lock = generate_lock([_salesforce_manifest()])
        drifts = verify_lock(lock, [_salesforce_manifest(), _slack_manifest()])
        assert any(d.kind == "added" and d.toolset_id == "slack" for d in drifts)

    def test_lock_serialization(self) -> None:
        lock = generate_lock([_salesforce_manifest()])
        json_str = lock.to_json()
        restored = ToolsetLock.from_json(json_str)
        assert restored.toolsets["salesforce"].version == "1.0.0"


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------


class TestCertification:
    @pytest.mark.asyncio
    async def test_well_formed_manifest_passes(self) -> None:
        result = await certify(_salesforce_manifest())
        assert isinstance(result, CertificationResult)
        assert result.certified is True
        assert result.passed_count == 12

    @pytest.mark.asyncio
    async def test_empty_manifest_fails(self) -> None:
        bad = ToolsetManifest(id="", version="", summary="")
        result = await certify(bad)
        assert result.certified is False
        assert result.failed_count > 0

    @pytest.mark.asyncio
    async def test_missing_scopes_fails(self) -> None:
        m = ToolsetManifest(
            id="test",
            version="1.0.0",
            summary="Test toolset",
            base_url="https://api.test.com",
            egress_hosts=["api.test.com"],
            fakes_module="test.fakes",
            rate_limits={"default": {"rps": 10}},
            groups={
                "ops": [
                    OperationSpec(
                        id="ops.create",
                        summary="Create something",
                        effect=EffectClass.WRITE,
                        input_schema={"type": "object"},
                        # Missing scopes for a WRITE op
                    ),
                ],
            },
        )
        result = await certify(m)
        failed_codes = [r.code for r in result.results if not r.passed]
        assert "CERT-05" in failed_codes

    @pytest.mark.asyncio
    async def test_bad_version_fails(self) -> None:
        m = _salesforce_manifest()
        m.version = "latest"
        result = await certify(m)
        failed_codes = [r.code for r in result.results if not r.passed]
        assert "CERT-12" in failed_codes

    @pytest.mark.asyncio
    async def test_no_egress_fails(self) -> None:
        m = _salesforce_manifest()
        m.egress_hosts = []
        result = await certify(m)
        failed_codes = [r.code for r in result.results if not r.passed]
        assert "CERT-07" in failed_codes
