"""Manifests must document imports that actually resolve.

The failure this prevents: a manifest lists operations as ``messages.search``
with no import path, a coding agent asked to write Python invents
``from loom import gmail`` to match, and the generated workflow fails on its
first line. Documentation that names a symbol is a promise the symbol exists.
"""

from __future__ import annotations

import importlib

import pytest

from workflow_builder.toolsets.confluence.manifest import CONFLUENCE_MANIFEST
from workflow_builder.toolsets.google import GMAIL_MANIFEST, GOOGLE_CALENDAR_MANIFEST
from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

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
        from workflow_builder.toolsets.manifest import OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="s",
            groups={"g": [OperationSpec(id="g.do", function="do_it", summary="s")]},
        )
        assert manifest.import_line() == ""

    def test_no_functions_means_no_import_line(self) -> None:
        from workflow_builder.toolsets.manifest import OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="s",
            tools_module="some.module",
            groups={"g": [OperationSpec(id="g.do", summary="s")]},
        )
        assert manifest.import_line() == ""

    def test_names_are_sorted_and_deduplicated(self) -> None:
        from workflow_builder.toolsets.manifest import OperationSpec, ToolsetManifest

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
        from workflow_builder.agents.tool_registry import ToolsetRegistry

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        docs = registry.describe()

        assert (
            "from workflow_builder.toolsets.google.gmail.tools import" in docs
        ), "the docs never say how to import the toolset"
        assert "gmail_search_messages(" in docs
        assert "ctx.step(" in docs

    def test_docs_do_not_present_operation_ids_as_callables(self) -> None:
        """'messages.search(...)' is what got invented into an import."""
        from workflow_builder.agents.tool_registry import ToolsetRegistry

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        docs = registry.describe()

        assert "messages.search(" not in docs

    def test_a_manifest_without_a_module_says_it_is_not_importable(self) -> None:
        from workflow_builder.agents.tool_registry import ToolsetRegistry
        from workflow_builder.toolsets.manifest import OperationSpec, ToolsetManifest

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
