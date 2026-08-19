"""Tests for the toolset subsystem — manifest, catalog, registry, connections,
rate limiting, lock/drift, and certification."""

from __future__ import annotations

import json

import pytest

from loom.toolsets.catalog import (
    IndexCard,
    OpContract,
    OpTable,
    ToolsetCatalog,
)
from loom.toolsets.certify import (
    CertificationResult,
    certify,
)
from loom.toolsets.connections import ConnectionBroker, Credential
from loom.toolsets.gateway import RateLimitConfig, RateLimiter
from loom.toolsets.lock import (
    ToolsetLock,
    generate_lock,
    verify_lock,
)
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.registry import (
    get_catalog,
    register_available_toolsets,
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
        tools_module="loom_toolset_salesforce.tools",
        rate_limits={"default": {"rps": 10}},
        groups={
            "leads": [
                OperationSpec(
                    id="leads.upsert",
                    function="salesforce_upsert_lead",
                    summary="Create or update a lead",
                    effect=EffectClass.WRITE,
                    input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
                    output_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                    scopes=["leads:write"],
                    idempotent=True,
                ),
                OperationSpec(
                    id="leads.search",
                    function="salesforce_search_leads",
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
                    function="salesforce_get_contact",
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
        from loom.core.exceptions import CredentialNotFound

        broker = ConnectionBroker()
        with pytest.raises(CredentialNotFound, match="No credential found"):
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
        assert cred.expired() is False

    def test_credential_expiry_uses_injected_clock(self) -> None:
        from datetime import UTC, datetime

        from loom.runtime.clock import ManualClock

        cred = Credential(
            token="test", expires_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        clock = ManualClock(datetime(2025, 12, 31, tzinfo=UTC))
        assert cred.expired(clock) is False

        clock.set(datetime(2026, 1, 2, tzinfo=UTC))
        assert cred.expired(clock) is True


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
        # Every check, rather than a count: the number changes whenever one is
        # added, and a magic number here made an added check look like a
        # regression in an unrelated file.
        assert result.passed_count == len(result.results)
        assert result.failed_count == 0

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
            auth={"type": "oauth2"},
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


class TestBuiltInToolsetsReachEveryProcess:
    """A generated workflow is run by a process nobody wrote.

    ``python generated_workflow.py`` builds ``Runtime.from_env()`` and calls
    ``ctx.agent(toolsets=["jira"])``. Nothing in that file registers a toolset,
    so the run died with "no executable toolset 'jira' is registered (known:
    none)" — which reads as a broken library rather than a missing call.

    They resolve **by name only**. Registering them eagerly was the first
    attempt and it was wrong: ``resolve_tools()`` sweeps every registered
    toolset when given no ids, so a prompt-only ``ctx.agent("summarise this")``
    was handed 46 tools including ``jira_delete_issue``. The grant tests caught
    it, correctly.
    """

    def test_a_named_toolset_resolves_with_nothing_registered(self) -> None:
        """The failing path, end to end."""
        from loom import Runtime

        tools = Runtime().toolsets.resolve_tools(["jira"])
        assert [t.name for t in tools][:1] == ["jira_search_issues"]

    @pytest.mark.parametrize(
        "toolset_id", ["jira", "confluence", "gmail", "google_calendar"]
    )
    def test_every_shipped_toolset_is_reachable(self, toolset_id: str) -> None:
        from loom.toolsets.registry import builtin_toolset

        toolset = builtin_toolset(toolset_id)
        assert toolset is not None
        assert toolset.manifest.id == toolset_id

    def test_naming_none_still_grants_none(self) -> None:
        """The regression the eager version introduced.

        An agent that named no integration must not acquire four of them, and
        certainly not their destructive operations.
        """
        from loom import Runtime

        registry = Runtime().toolsets
        assert registry.resolve_tools() == []
        assert registry.list_toolsets() == []

    def test_a_hosts_own_toolset_wins(self) -> None:
        """A different Jira — another account, another base URL — is not shadowed."""
        from loom.agents.tool_registry import Toolset, ToolsetRegistry
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        registry = ToolsetRegistry()
        mine = Toolset(manifest=JIRA_MANIFEST, _resolver=lambda op: f"mine:{op}")
        registry.register(mine)

        assert registry.get_toolset("jira") is mine

    def test_builtin_fallback_can_be_disabled(self) -> None:
        """Multi-tenant hosts must not resolve unknown ids to env-credentialed builtins."""
        from loom.agents.tool_registry import ToolsetRegistry

        open_registry = ToolsetRegistry()
        assert open_registry.get_toolset("jira") is not None

        closed = ToolsetRegistry(allow_builtin_fallback=False)
        assert closed.get_toolset("jira") is None

    def test_an_unknown_toolset_is_still_unknown(self) -> None:
        from loom.toolsets.registry import builtin_toolset

        assert builtin_toolset("not_a_real_toolset") is None

    def test_resolving_imports_no_toolset_code_until_asked(self) -> None:
        """Four eager imports would undo the lazy catalog this lives beside.

        Manifests are metadata; the client, its httpx, and its auth load on the
        first resolve and not before.
        """
        import subprocess
        import sys

        module = "loom.toolsets.jira.tools"
        probe = (
            "import sys;"
            "from loom import Runtime;"
            "rt = Runtime();"
            f"before = '{module}' in sys.modules;"
            "rt.toolsets.resolve_tools(['jira']);"
            f"print(before, '{module}' in sys.modules)"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        assert done.stdout.strip() == "False True", done.stdout

    def test_an_unknown_operation_says_what_exists(self) -> None:
        from loom.toolsets.registry import builtin_toolset

        with pytest.raises(KeyError, match="known:"):
            builtin_toolset("jira").resolve("issues.telepathy")


class TestRegisterAvailableToolsets:
    """``loom mcp`` seeds the catalog; a bare Runtime does not."""

    def test_seeds_every_shipped_toolset(self) -> None:
        """Derived from BUILTIN_TOOLSETS, not repeated from it.

        This assertion used to be a literal list of four ids, and it had
        already gone stale — ``google.drive`` and ``google.meet`` were shipping
        and unnamed here, so "every shipped toolset" was checking four of six.
        A list that has to be edited in step with the source it describes gets
        edited late or not at all.
        """
        from loom.toolsets.registry import BUILTIN_TOOLSETS

        ids = register_available_toolsets()
        catalog = get_catalog()

        for toolset_id in ids:
            assert catalog.get(toolset_id) is not None
            assert catalog.get_toolset(toolset_id) is not None

        shipped = {
            getattr(
                __import__(path.rsplit(".", 1)[0], fromlist=["x"]),
                path.rsplit(".", 1)[1],
            ).id
            for path, _ in BUILTIN_TOOLSETS
        }
        assert shipped <= set(catalog.toolset_ids), (
            f"shipped but not seeded: {sorted(shipped - set(catalog.toolset_ids))}"
        )

    def test_a_second_call_does_not_duplicate_or_replace(self) -> None:
        first = register_available_toolsets()
        jira = get_catalog().get_toolset("jira")
        second = register_available_toolsets()
        assert second == first
        assert get_catalog().get_toolset("jira") is jira

    def test_does_not_overwrite_a_host_registration(self) -> None:
        from loom.agents.tool_registry import Toolset
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        mine = Toolset(manifest=JIRA_MANIFEST, _resolver=lambda op: f"mine:{op}")
        register_toolset(mine)
        register_available_toolsets()
        assert get_catalog().get_toolset("jira") is mine


class TestAwaitingADurableCallIsTyped:
    """``__await__`` returned ``Any``, so every awaited call was untyped.

    A workflow returning an agent's reply from a ``-> str`` body drew
    ``no-any-return`` from mypy — on the exact pattern the coding agent is told
    to write, in every generation that used an agent. Checked through the real
    type stage, because the claim is about what mypy says.
    """

    HEAD = "from loom import Context, workflow\n\n\n"

    async def _types(self, body: str) -> list[str]:
        from loom.agents.checks import CheckContext
        from loom.agents.stages import TypeStage

        code = (
            f'{self.HEAD}@workflow(name="w")\n'
            f"async def w(ctx: Context, _=None) -> str:\n"
            f'    """Report."""\n'
            f"    r = await ctx.agent(\"hi\")\n"
            f"    {body}\n"
        )
        result = await TypeStage().run(code, CheckContext())
        return [issue.message for issue in result.issues]

    async def test_returning_the_text_is_clean(self) -> None:
        """What the prompt now teaches."""
        assert await self._types("return r.text()") == []

    async def test_returning_the_output_is_a_real_narrowing(self) -> None:
        """Not silence — mypy is right that the output may be absent.

        The old ``Any`` hid this. The complaint is now accurate, which is why
        the prompt points at ``text()`` rather than the type being loosened
        back until it stops talking.
        """
        problems = await self._types("return r.output")

        assert problems, "output is Optional; mypy should say so"
        assert "no-any-return" not in " ".join(problems), "should not be Any any more"


# ---------------------------------------------------------------------------
# Effect classification defaults (Phase 12)
# ---------------------------------------------------------------------------


class TestEffectDefaultFailsSafe:
    """``effect`` defaults to the cautious end of the scale.

    READ is the one class exempt from every write and destructive control, so
    defaulting to it meant an operation nobody classified was *granted* rather
    than flagged."""

    def test_an_unclassified_operation_is_a_write(self) -> None:
        op = OperationSpec(id="pages.nuke", summary="permanently delete every page")
        assert op.effect is EffectClass.WRITE

    def test_the_manifest_now_agrees_with_the_broker(self) -> None:
        """``EffectCall.effect`` has always defaulted to WRITE. The manifest
        defaulting to READ meant the two disagreed about the same question."""
        from loom.runtime.effects import EffectCall

        assert OperationSpec(id="x.y", summary="s").effect == EffectCall(
            kind="step", target="x.y"
        ).effect

    def test_declaration_is_distinguishable_from_the_default(self) -> None:
        """The mechanism CERT-04 depends on."""
        assert "effect" not in OperationSpec(id="x.y", summary="s").model_fields_set
        assert "effect" in OperationSpec(
            id="x.y", summary="s", effect=EffectClass.WRITE
        ).model_fields_set

    @pytest.mark.parametrize(
        "build",
        [
            lambda d: OperationSpec(**d),
            lambda d: OperationSpec.model_validate(d),
            lambda d: OperationSpec.model_validate_json(json.dumps(d)),
        ],
        ids=["constructed", "from_dict", "from_json"],
    )
    def test_declaration_survives_every_construction_path(self, build) -> None:
        """A third-party manifest arrives through an entry point as data, not
        as a Python literal. If this stopped holding, CERT-04 would read every
        such manifest as unclassified."""
        base = {"id": "x.y", "summary": "s"}
        assert "effect" not in build(base).model_fields_set
        assert "effect" in build({**base, "effect": "read"}).model_fields_set


class TestCertificationCatchesUnclassifiedOperations:
    """CERT-04 claimed to require an explicit classification and could not fail:
    it tested ``if not op.effect``, and every ``EffectClass`` member is truthy."""

    @pytest.mark.asyncio
    async def test_an_undeclared_effect_now_fails_cert_04(self) -> None:
        m = ToolsetManifest(
            id="t",
            version="1.0.0",
            summary="s",
            base_url="https://api.test.com",
            egress_hosts=["api.test.com"],
            fakes_module="t.fakes",
            rate_limits={"default": {"rps": 10}},
            groups={
                "pages": [
                    OperationSpec(
                        id="pages.nuke",
                        summary="permanently delete every page",
                        input_schema={"type": "object"},
                        scopes=["pages:write"],
                    )
                ]
            },
        )
        result = await certify(m)
        failed = {r.code for r in result.results if not r.passed}
        assert "CERT-04" in failed
        assert result.certified is False

    @pytest.mark.asyncio
    async def test_an_unclassified_operation_is_no_longer_scope_exempt(self) -> None:
        """CERT-05 only demanded scopes for write/destructive. Under the READ
        default an unclassified operation was exempt from that too, so one
        omission disarmed both checks."""
        m = ToolsetManifest(
            id="t",
            version="1.0.0",
            summary="s",
            auth={"type": "oauth2"},
            base_url="https://api.test.com",
            egress_hosts=["api.test.com"],
            tools_module="t.tools",
            rate_limits={"default": {"rps": 10}},
            groups={
                "pages": [
                    OperationSpec(
                        id="pages.nuke",
                        function="t_nuke",
                        summary="permanently delete every page",
                        input_schema={"type": "object"},
                    )
                ]
            },
        )
        result = await certify(m)
        failed = {r.code for r in result.results if not r.passed}
        assert {"CERT-04", "CERT-05"} <= failed

    @pytest.mark.asyncio
    async def test_a_declared_manifest_still_certifies(self) -> None:
        result = await certify(_salesforce_manifest())
        assert result.certified is True


class TestShippedToolsetsAreClassified:
    """The corpus guard. Every operation LOOM ships declares its own effect, so
    none of them reaches the default — and a new one that forgets is caught
    here rather than by whatever it is later granted."""

    def test_every_shipped_operation_declares_its_effect(self) -> None:
        import importlib
        import pkgutil

        import loom.toolsets as pkg

        undeclared = []
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if not mod.name.endswith(".manifest"):
                continue
            try:
                loaded = importlib.import_module(mod.name)
            except Exception:  # pragma: no cover - optional extras
                continue
            for value in vars(loaded).values():
                for manifest in value if isinstance(value, list | tuple) else [value]:
                    if type(manifest).__name__ != "ToolsetManifest":
                        continue
                    undeclared += [
                        f"{manifest.id}.{op.id}"
                        for op in manifest.all_operations()
                        if "effect" not in op.model_fields_set
                    ]
        assert not undeclared, f"operations relying on the default: {undeclared}"


class TestFakeabilityCertification:
    """CERT-08 used to demand a hand-written ``fakes_module`` and so failed all
    23 shipped toolsets, none of which declares one — deliberately, because
    ``agents/fakes.py`` generates stand-ins from ``output_schema`` so that no
    parallel set of fakes can drift from the contract."""

    def _manifest(self, **kw) -> ToolsetManifest:
        defaults = dict(
            id="t",
            version="1.0.0",
            summary="s",
            base_url="https://api.test.com",
            egress_hosts=["api.test.com"],
            rate_limits={"default": {"rps": 10}},
            tools_module="t.tools",
            groups={
                "g": [
                    OperationSpec(
                        id="g.read",
                        summary="Read.",
                        function="t_read",
                        effect=EffectClass.READ,
                        input_schema={"type": "object"},
                        output_schema={"type": "object"},
                    )
                ]
            },
        )
        defaults.update(kw)
        return ToolsetManifest(**defaults)

    @pytest.mark.asyncio
    async def test_a_generated_fake_is_enough(self) -> None:
        """No fakes_module, and that is the normal case rather than a defect."""
        result = await certify(self._manifest())
        cert08 = next(r for r in result.results if r.code == "CERT-08")
        assert cert08.passed is True

    @pytest.mark.asyncio
    async def test_no_tools_module_fails(self) -> None:
        """Without it ``install_fakes`` returns [] and the smoke sandbox runs
        against the real service — a 401 the repair loop escapes by deleting
        the integration."""
        result = await certify(self._manifest(tools_module=""))
        assert "CERT-08" in {r.code for r in result.results if not r.passed}

    @pytest.mark.asyncio
    async def test_an_operation_with_no_function_fails(self) -> None:
        m = self._manifest(
            groups={"g": [OperationSpec(id="g.x", summary="x", effect=EffectClass.READ)]}
        )
        result = await certify(m)
        failed = {r.code for r in result.results if not r.passed}
        assert "CERT-08" in failed

    @pytest.mark.asyncio
    async def test_every_shipped_toolset_is_fakeable(self) -> None:
        """The check exists to protect the smoke sandbox, and the sandbox is
        what every shipped toolset is exercised through."""
        import importlib
        import pkgutil

        import loom.toolsets as pkg

        failures = []
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if not mod.name.endswith(".manifest"):
                continue
            try:
                loaded = importlib.import_module(mod.name)
            except Exception:  # pragma: no cover - optional extras
                continue
            for value in vars(loaded).values():
                for man in value if isinstance(value, list | tuple) else [value]:
                    if type(man).__name__ != "ToolsetManifest":
                        continue
                    result = await certify(man)
                    if any(
                        r.code == "CERT-08" and not r.passed for r in result.results
                    ):
                        failures.append(man.id)
        assert not failures, f"toolsets that cannot be faked: {sorted(set(failures))}"

    @pytest.mark.asyncio
    async def test_the_check_agrees_with_install_fakes(self) -> None:
        """The premise, verified against the code it models rather than
        restated. A manifest CERT-08 passes must actually yield fakes."""
        from loom.agents.fakes import install_fakes, uninstall_fakes
        from loom.toolsets.google.gmail.manifest import GMAIL_MANIFEST

        result = await certify(GMAIL_MANIFEST)
        assert next(r for r in result.results if r.code == "CERT-08").passed
        replaced = install_fakes(GMAIL_MANIFEST)
        try:
            assert replaced, "CERT-08 passed but install_fakes replaced nothing"
        finally:
            uninstall_fakes()


class TestEveryShippedToolsetCertifies:
    """The gate. Certification went 0/23 → 23/23 by fixing two checks that
    demanded things the architecture does not have, and filling the two gaps
    that were real once those stopped masking them."""

    @pytest.mark.asyncio
    async def test_all_of_them(self) -> None:
        import importlib
        import pkgutil

        import loom.toolsets as pkg

        failures: dict[str, list[str]] = {}
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if not mod.name.endswith(".manifest"):
                continue
            try:
                loaded = importlib.import_module(mod.name)
            except Exception:  # pragma: no cover - optional extras
                continue
            for value in vars(loaded).values():
                for man in value if isinstance(value, list | tuple) else [value]:
                    if type(man).__name__ != "ToolsetManifest":
                        continue
                    result = await certify(man)
                    if not result.certified:
                        failures[man.id] = [
                            f"{r.code}: {r.reason}"
                            for r in result.results
                            if not r.passed
                        ]
        assert not failures, f"toolsets no longer certify: {failures}"

    @pytest.mark.asyncio
    async def test_no_write_carries_a_read_only_scope(self) -> None:
        """A scope narrower than the operation is a runtime 403 that reads as a
        broken toolset. ``Sites.ReadWrite.All`` contains ``.read``, so this is
        checked with the predicate rather than a substring."""
        import importlib
        import pkgutil

        import loom.toolsets as pkg
        from loom.toolsets.effects import scope_is_readonly

        wrong = []
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if not mod.name.endswith(".manifest"):
                continue
            try:
                loaded = importlib.import_module(mod.name)
            except Exception:  # pragma: no cover - optional extras
                continue
            for value in vars(loaded).values():
                for man in value if isinstance(value, list | tuple) else [value]:
                    if type(man).__name__ != "ToolsetManifest":
                        continue
                    wrong += [
                        f"{man.id}.{op.id}"
                        for op in man.all_operations()
                        if op.effect is not EffectClass.READ
                        and scope_is_readonly(op.scopes)
                    ]
        assert not wrong, f"write/destructive ops with a read-only scope: {wrong}"
