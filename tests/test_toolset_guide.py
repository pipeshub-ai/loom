"""Build a toolset by following the guide, and check that it works.

Every snippet in ``docs/guides/toolsets.md`` executes in CI, which proves each
one is valid Python and proves nothing about whether *following the guide* gets
you a working toolset. The steps could each run and still not compose — a
manifest that never reaches the registry, a paging style that never sees a
cursor, fakes that the sandbox cannot find.

So this writes the three files the guide describes, in a directory with nothing
else in it, imports them as a package, and drives the result through every path
a real toolset is used by: registration, discovery, resolution, execution,
pagination, and the fakes the coding agent's sandbox depends on.

If the guide changes and stops being true, this fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --- the three files, as the guide lays them out ---------------------------

CLIENT = '''
"""Step 1-2: credentials, then error classification."""
from __future__ import annotations

import os
from typing import Any

from workflow_builder.core.exceptions import ConfigurationError, NonRetryableError, WorkflowError
from workflow_builder.toolsets.pagination import OffsetPaging, Results, page_through


class MyApiError(WorkflowError):
    """Anything the service returned as a failure."""


class MyPermanentError(MyApiError, NonRetryableError):
    """A 4xx. Retrying changes nothing, so Retry stops on it."""


ROWS = [{"id": str(i), "title": f"record {i}"} for i in range(250)]
PAGE_CAP = 50


class MyClient:
    def __init__(self, base_url: str = "", api_token: str = "") -> None:
        self._base_url = (base_url or os.environ.get("MYSVC_URL", "")).rstrip("/")
        self._token = api_token or os.environ.get("MYSVC_TOKEN", "")
        if not self._base_url:
            raise ConfigurationError(
                "MYSVC_URL is required (env var or base_url argument)"
            )

    async def _get(self, path: str, **params: Any) -> Any:
        """Stands in for the HTTP call. Offset-paged, capped server-side."""
        start = int(params.get("startAt", 0))
        size = min(int(params.get("maxResults", 25)), PAGE_CAP)
        return {"records": ROWS[start : start + size], "total": len(ROWS)}

    async def search_records(self, query: str, max_results: int = 20) -> Results:
        """Step 5 of the pagination section: one call, one style."""
        return await page_through(
            lambda asked: self._get("records", **asked),
            style=OffsetPaging(items="records", total_field="total"),
            limit=max_results,
            page_size=PAGE_CAP,
            row=lambda raw: raw["title"],
        )

    async def get_record(self, record_id: str) -> str:
        return f"record {record_id}"


_default_client: MyClient | None = None


def get_default_client() -> MyClient:
    """Return (or create) the module-level client from env vars."""
    global _default_client
    if _default_client is None:
        _default_client = MyClient()
    return _default_client
'''

TOOLS = '''
"""The callable surface: @step functions."""
from __future__ import annotations

from workflow_builder import Retry, step
from workflow_builder.toolsets.pagination import Results


@step(retry=Retry(max_attempts=3))
async def mysvc_search_records(query: str, max_results: int = 20) -> Results[str]:
    """Search records.

    Args:
        query: What to look for.
        max_results: Most rows to return.
    """
    from mysvc.client import get_default_client

    return await get_default_client().search_records(query, max_results)


@step
async def mysvc_get_record(record_id: str) -> str:
    """Fetch one record.

    Args:
        record_id: Which record.
    """
    from mysvc.client import get_default_client

    return await get_default_client().get_record(record_id)
'''

MANIFEST = '''
"""Pure data. Imports nothing heavy — the catalog loads this eagerly."""
from __future__ import annotations

from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

MY_MANIFEST = ToolsetManifest(
    id="mysvc",
    version="1.0.0",
    summary="MyService — search and fetch records.",
    tools_module="mysvc.tools",
    groups={
        "records": [
            OperationSpec(
                id="records.search",
                function="mysvc_search_records",
                summary="Search records.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                resolves="record",
                output_schema={"type": "array", "items": {"type": "string"}},
            ),
            OperationSpec(
                id="records.get",
                function="mysvc_get_record",
                summary="Fetch one record.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema={"type": "string"},
            ),
        ]
    },
)
'''


@pytest.fixture(scope="module")
def toolset(tmp_path_factory):
    """The package, written and imported exactly as a third party would ship it."""
    root = tmp_path_factory.mktemp("guide")
    package = root / "mysvc"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "client.py").write_text(CLIENT)
    (package / "tools.py").write_text(TOOLS)
    (package / "manifest.py").write_text(MANIFEST)

    sys.path.insert(0, str(root))
    try:
        import mysvc.manifest as manifest_module
        import mysvc.tools as tools_module

        yield manifest_module.MY_MANIFEST, tools_module
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules if n.startswith("mysvc")]:
            del sys.modules[name]


# ---------------------------------------------------------------------------


class TestTheGuideProducesAWorkingToolset:
    def test_credentials_fail_in_the_constructor_naming_the_variable(
        self, toolset, monkeypatch
    ) -> None:
        """Step 1. Not on the first request, five frames into a step."""
        import mysvc.client as client

        from workflow_builder.core.exceptions import ConfigurationError

        monkeypatch.delenv("MYSVC_URL", raising=False)
        with pytest.raises(ConfigurationError, match="MYSVC_URL is required"):
            client.MyClient()

        assert client.MyClient(base_url="https://x").__class__ is client.MyClient

    def test_the_error_hierarchy_imports(self, toolset) -> None:
        """Step 2. The flat two-base form has no MRO and fails at import."""
        import mysvc.client as client

        from workflow_builder.core.exceptions import NonRetryableError

        assert issubclass(client.MyPermanentError, NonRetryableError)
        assert issubclass(client.MyPermanentError, client.MyApiError)

    def test_the_manifest_imports_nothing_heavy(self, toolset, tmp_path) -> None:
        """Step 5. The catalog loads every manifest; code waits for resolve.

        Checked in a fresh interpreter, because this one already imported the
        client — asserting against ``sys.modules`` here could only ever be
        vacuous, which is worse than not checking.
        """
        import subprocess

        manifest, _ = toolset
        assert manifest.tools_module == "mysvc.tools"
        assert manifest.import_line(), "generated code needs an import line"

        root = str(Path(sys.modules["mysvc.manifest"].__file__).parents[1])
        probe = (
            "import sys;"
            f"sys.path.insert(0, {root!r});"
            "import mysvc.manifest;"
            "print('mysvc.client' in sys.modules)"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        assert done.stdout.strip() == "False", "the manifest pulled in the client"

    def test_declared_imports_actually_resolve(self, toolset) -> None:
        """Step 5, the rule test_manifest_imports enforces for shipped toolsets."""
        import importlib

        manifest, _ = toolset
        module = importlib.import_module(manifest.tools_module)
        for op in manifest.all_operations():
            assert hasattr(module, op.function), f"{op.id} names a missing function"

    def test_registration_makes_it_discoverable_and_callable(self, toolset) -> None:
        """Step 6."""
        from workflow_builder.agents.tool_registry import Toolset, ToolsetRegistry

        _, tools = toolset
        registry = ToolsetRegistry()
        registry.register(
            Toolset.from_steps(
                "mysvc", [tools.mysvc_search_records, tools.mysvc_get_record]
            )
        )

        assert "mysvc" in registry.list_toolsets()
        assert registry.get_toolset("mysvc") is not None
        assert registry.resolve_tools(["mysvc"])

    def test_the_agent_is_told_what_it_needs(self, toolset) -> None:
        """Steps 3-5 reaching the model: paging, resolvers, effect classes."""
        from workflow_builder.agents.tool_registry import ToolsetRegistry

        manifest, _ = toolset
        registry = ToolsetRegistry()
        registry.register(manifest)
        docs = registry.describe(["mysvc"], detail="index")

        assert "Paged: mysvc_search_records" in docs
        assert "Resolve a record with mysvc_search_records" in docs

    async def test_pagination_follows_pages_and_reports_coverage(
        self, toolset
    ) -> None:
        """Step 5 of the pagination section, against a server that caps at 50."""
        import mysvc.client as client

        service = client.MyClient(base_url="https://x")

        capped = await service.search_records("q", max_results=120)
        assert len(capped) == 120, "the 50-row server cap must not shorten it"
        assert not capped.complete
        assert capped.summary() == "120 of 250"

        everything = await service.search_records("q", max_results=250)
        assert everything.complete

    async def test_the_result_survives_a_journal(self, toolset) -> None:
        """Coverage must outlive being returned from a step."""
        import mysvc.client as client

        from workflow_builder.core.serde import decode, encode

        found = await client.MyClient(base_url="https://x").search_records("q", 60)
        restored = decode(encode(found))

        assert restored.total == 250
        assert restored.summary() == "60 of 250"

    def test_fakes_come_from_the_declared_schema(self, toolset) -> None:
        """Step 7. Without these the sandbox reaches a 401 and proves nothing."""
        from workflow_builder.agents.fakes import install_fakes, uninstall_fakes

        manifest, tools = toolset
        real = tools.mysvc_get_record
        try:
            replaced = install_fakes(manifest)
            assert "mysvc_get_record" in replaced
            assert tools.mysvc_get_record is not real
        finally:
            uninstall_fakes()
        assert tools.mysvc_get_record is real

    async def test_a_workflow_can_use_it_end_to_end(self, toolset) -> None:
        """The thing all of the above is for."""
        import mysvc.client as client

        from workflow_builder import Context, Runtime, workflow
        from workflow_builder.state import MemoryStore

        client._default_client = client.MyClient(base_url="https://x")

        @workflow(name="guide_report")
        async def report(ctx: Context, _=None) -> str:
            """Report on records, honestly about coverage."""
            _, tools = toolset
            found = await ctx.step(tools.mysvc_search_records, "q", max_results=80)
            return f"{found.summary()}: {found[0]}"

        runtime = Runtime(store=MemoryStore())
        runtime.register(report)
        result = await runtime.run(report)

        assert result.status.value == "completed"
        assert result.output == "80 of 250: record 0"
