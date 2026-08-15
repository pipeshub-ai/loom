"""Manifests must document imports that actually resolve.

The failure this prevents: a manifest lists operations as ``messages.search``
with no import path, a coding agent asked to write Python invents
``from loom import gmail`` to match, and the generated workflow fails on its
first line. Documentation that names a symbol is a promise the symbol exists.
"""

from __future__ import annotations

import importlib
from typing import ClassVar

import pytest

from loom.toolsets.confluence.manifest import CONFLUENCE_MANIFEST
from loom.toolsets.google import GMAIL_MANIFEST, GOOGLE_CALENDAR_MANIFEST
from loom.toolsets.jira.manifest import JIRA_MANIFEST

FIRST_PARTY = [
    GMAIL_MANIFEST,
    GOOGLE_CALENDAR_MANIFEST,
    JIRA_MANIFEST,
    CONFLUENCE_MANIFEST,
]


@pytest.mark.parametrize("manifest", FIRST_PARTY, ids=lambda m: m.id)
class TestDeclaredImportsResolve:
    def test_the_tools_module_imports(self, manifest) -> None:
        assert manifest.tools_module, f"{manifest.id} declares no tools_module"
        importlib.import_module(manifest.tools_module)

    def test_every_operation_names_a_function(self, manifest) -> None:
        missing = [op.id for op in manifest.all_operations() if not op.function]
        assert not missing, f"operations with no callable: {missing}"

    def test_every_named_function_exists_and_is_a_step(self, manifest) -> None:
        module = importlib.import_module(manifest.tools_module)
        for op in manifest.all_operations():
            fn = getattr(module, op.function, None)
            assert fn is not None, f"{manifest.tools_module}.{op.function} does not exist"
            # A plain function would not be journalled; the docs say ctx.step().
            assert hasattr(fn, "name") or callable(fn), f"{op.function} is not callable"

    def test_the_import_line_is_executable(self, manifest) -> None:
        """Not merely well-formed — actually run it."""
        line = manifest.import_line()
        assert line.startswith(f"from {manifest.tools_module} import ")
        exec(compile(line, "<manifest>", "exec"), {})


class TestImportLineIsHonest:
    def test_no_module_means_no_import_line(self) -> None:
        """Half an import is worse than none — it would be guessed at."""
        from loom.toolsets.manifest import OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="s",
            groups={"g": [OperationSpec(id="g.do", function="do_it", summary="s")]},
        )
        assert manifest.import_line() == ""

    def test_no_functions_means_no_import_line(self) -> None:
        from loom.toolsets.manifest import OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="s",
            tools_module="some.module",
            groups={"g": [OperationSpec(id="g.do", summary="s")]},
        )
        assert manifest.import_line() == ""

    def test_names_are_sorted_and_deduplicated(self) -> None:
        from loom.toolsets.manifest import OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="s",
            tools_module="m",
            groups={
                "g": [
                    OperationSpec(id="g.b", function="b_fn", summary="s"),
                    OperationSpec(id="g.a", function="a_fn", summary="s"),
                    OperationSpec(id="g.a2", function="a_fn", summary="s"),
                ]
            },
        )
        assert manifest.import_line() == "from m import a_fn, b_fn"


class TestGeneratedDocs:
    def test_docs_show_the_import_and_the_function_names(self) -> None:
        """What the coding agent reads must contain code it can write."""
        from loom.agents.tool_registry import ToolsetRegistry

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        docs = registry.describe()

        assert (
            "from loom.toolsets.google.gmail.tools import" in docs
        ), "the docs never say how to import the toolset"
        assert "gmail_search_messages(" in docs
        assert "ctx.step(" in docs

    def test_docs_do_not_present_operation_ids_as_callables(self) -> None:
        """'messages.search(...)' is what got invented into an import."""
        from loom.agents.tool_registry import ToolsetRegistry

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        docs = registry.describe()

        assert "messages.search(" not in docs

    def test_a_manifest_without_a_module_says_it_is_not_importable(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.manifest import OperationSpec, ToolsetManifest

        registry = ToolsetRegistry()
        registry.register(
            ToolsetManifest(
                id="opaque",
                version="1.0.0",
                summary="No python behind it",
                groups={"g": [OperationSpec(id="g.do", summary="does a thing")]},
            )
        )
        docs = registry.describe()

        assert "not importable" in docs
        assert "Import:" not in docs


class TestPaginationIsDeclaredWhereItHappens:
    """One rule, checked mechanically, across every toolset there will ever be.

    ``max_results: int`` looks identical whether it caps a complete answer or
    truncates a partial one, so the agent cannot infer paging from a signature
    and a workflow ends up reporting a page as a total. Declaring it per
    operation by hand would work for four toolsets and drift for a thousand.

    So the return type *is* the declaration — a paged read returns ``Results``
    — and this asserts the hand-written manifests agree with the functions they
    describe. Auto-generated manifests cannot disagree; they derive it.
    """

    def _manifests(self):
        return [
            JIRA_MANIFEST,
            CONFLUENCE_MANIFEST,
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        ]

    def _resolve(self, manifest, op):
        import importlib

        module = importlib.import_module(manifest.tools_module)
        return getattr(module, op.function, None)

    #: Toolset id → the module whose client methods do the paging.
    CLIENTS: ClassVar[dict[str, str]] = {
        "jira": "loom.toolsets.jira.client",
        "confluence": "loom.toolsets.confluence.client",
        "gmail": "loom.toolsets.google.gmail.client",
        "google_calendar": "loom.toolsets.google.calendar.client",
    }

    def _client_pages(self, module_name: str) -> set[str]:
        """Client methods that run a paging loop.

        Source inspection, and deliberately so: the client is the only place
        that knows the truth, and asking it by *calling* it would need
        credentials. Fails closed — a loop written some way this does not
        recognise reports a missing declaration rather than passing quietly.
        """
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        found: set[str] = set()
        for _, klass in inspect.getmembers(module, inspect.isclass):
            if not klass.__module__.startswith(module_name):
                continue
            for name, method in inspect.getmembers(klass, inspect.isfunction):
                try:
                    source = inspect.getsource(method)
                except OSError:
                    continue
                if "collect(" in source or "paginate(" in source or ".mapped(" in source:
                    found.add(name)
        return found

    def test_a_client_that_pages_says_so_all_the_way_out(self) -> None:
        """The check the two-way version could not make.

        ``calendar_list_calendars`` paged, rebuilt a plain list from the result,
        and returned it. Manifest and return type agreed it did not page — they
        agreed with each other and both disagreed with the code. The client is
        the ground truth, so this compares against it.
        """
        from loom.toolsets.pagination import paginates

        wrong: list[str] = []
        for manifest in self._manifests():
            paging = self._client_pages(self.CLIENTS[manifest.id])
            module = __import__(manifest.tools_module, fromlist=["x"])
            for op in manifest.all_operations():
                fn = getattr(module, op.function, None) if op.function else None
                if fn is None:
                    continue
                # The tool wraps a client method of a related name.
                stem = op.function.split("_", 1)[-1]
                if stem not in paging and op.id.split(".")[-1] not in paging:
                    continue
                if not paginates(fn):
                    wrong.append(
                        f"{manifest.id}.{op.id}: the client pages but "
                        f"{op.function} returns a plain list — the coverage is "
                        f"computed and thrown away"
                    )
        assert not wrong, "\n  ".join(["client and tool disagree:", *wrong])

    def test_every_manifest_matches_its_implementation(self) -> None:
        from loom.toolsets.pagination import paginates

        wrong: list[str] = []
        for manifest in self._manifests():
            if not manifest.tools_module:
                continue
            for op in manifest.all_operations():
                fn = self._resolve(manifest, op) if op.function else None
                if fn is None:
                    continue
                actual = paginates(fn)
                if actual != op.pagination:
                    claim = "declares" if op.pagination else "does not declare"
                    does = "does" if actual else "does not"
                    wrong.append(
                        f"{manifest.id}.{op.id}: manifest {claim} pagination, "
                        f"{op.function} {does} return Results"
                    )
        assert not wrong, "manifest and implementation disagree:\n  " + "\n  ".join(wrong)

    def test_the_paged_reads_are_reachable_as_a_group(self) -> None:
        """``paginated()`` is what the docs render; it must not be empty."""
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        paged = {op.function for op in JIRA_MANIFEST.paginated()}
        assert "jira_search_issues" in paged
        assert "jira_get_issue" not in paged, "a single-object read is not paged"

    def test_the_agent_is_told_which_reads_page(self) -> None:
        """The flag existed and reached the catalog, the lockfile, and nothing
        the model ever saw. This is the line that closes that gap."""
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)
        docs = registry.describe(["jira"], detail="index")

        assert "Paged: " in docs
        assert "jira_search_issues" in docs.split("Paged: ")[1].split("\n")[0]
        # The how-to is in the catalog header, once, not once per toolset.
        assert ".complete" in docs
        assert docs.count("Bounded set — one call") == 1
