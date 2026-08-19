"""The docs handed to the coding agent must describe the code that exists.

``get_tool_docs`` is step 3 of the agent's own process — "DOCS: get_tool_docs
for exact imports and signatures" — so a wrong line there is not a stale
comment, it is an instruction. And these docs are the one agent-facing surface
with no derivation behind it: ``registry.describe()`` is generated from
manifests and checked by ``test_manifest_imports.py``, while ``*_TOOL_DOCS`` is
hand-written prose around a few interpolated field lists.

It drifted, exactly as unguarded prose does. The Jira docs told the agent to
"loop with the cursor to fetch everything" against a tool that has no cursor
parameter and never had one — while the system prompt says flatly that no read
takes one. Nothing failed, because nothing was looking.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest

#: The toolsets whose docs the coding agent can fetch, named rather than
#: discovered.
#:
#: Discovering them at import time worked and was wrong: registering the
#: catalogue and importing every tools module during *collection* reorders
#: what the rest of the suite imports, and the first thing that surfaced was
#: an unrelated toolset failing to build a schema. A test file that changes
#: the conditions other test files run under is a test file that reports on
#: itself. :meth:`TestTheListIsComplete` is what keeps this honest — a newly
#: documented toolset fails there rather than being silently unchecked.
DOCUMENTED: tuple[str, ...] = (
    "confluence",
    "gmail",
    "google_calendar",
    "google_drive",
    "google_meet",
    "jira",
    "slack",
    "zoom",
)

#: A documented signature line: ``name(params) -> Return``, at column zero.
SIGNATURE = re.compile(r"^(\w+)\((.*)\)\s*->\s*(.+)$")


def _docs_and_module(toolset_id: str) -> tuple[str, Any]:
    """The docs string ``get_tool_docs`` would return, and the module behind it."""
    import importlib

    from loom.agents.coding_tools import _TOOL_DOCS_REGISTRY, _ensure_builtin_docs
    from loom.toolsets.registry import get_catalog, register_available_toolsets

    _ensure_builtin_docs()
    register_available_toolsets()

    docs = _TOOL_DOCS_REGISTRY[toolset_id]
    rendered = str(docs() if callable(docs) else docs)
    manifest = get_catalog().get(toolset_id)
    assert manifest is not None and manifest.tools_module
    return rendered, importlib.import_module(manifest.tools_module)


def _documented_signatures(docs: str) -> dict[str, tuple[list[str], str]]:
    """Function name -> (parameter names, return type) as the docs claim them."""
    found: dict[str, tuple[list[str], str]] = {}
    for line in docs.splitlines():
        match = SIGNATURE.match(line)
        if not match:
            continue
        name, params, returns = match.groups()
        names = []
        depth = 0
        current = ""
        for char in params:
            if char in "[({":
                depth += 1
            elif char in "])}":
                depth -= 1
            if char == "," and depth == 0:
                names.append(current)
                current = ""
            else:
                current += char
        names.append(current)
        cleaned = [
            p.strip().split(":")[0].split("=")[0].strip()
            for p in names
            if p.strip()
        ]
        # These docs align a trailing note after the return type
        # (``-> str         (permanent, not recoverable)``). Two or more
        # spaces is the separator every one of them uses; one space cannot be,
        # because ``SlackChannel | None`` contains them.
        found[name] = (cleaned, re.split(r"\s{2,}", returns.strip())[0].strip())
    return found


@pytest.fixture(params=DOCUMENTED, ids=DOCUMENTED)
def documented(request: pytest.FixtureRequest) -> tuple[str, str, Any]:
    docs, module = _docs_and_module(request.param)
    return request.param, docs, module


class TestTheListIsComplete:
    """A toolset that starts publishing docs must not go unchecked."""

    def test_every_documented_toolset_is_enrolled(self) -> None:
        from loom.agents.coding_tools import _TOOL_DOCS_REGISTRY, _ensure_builtin_docs
        from loom.toolsets.registry import get_catalog, register_available_toolsets

        _ensure_builtin_docs()
        register_available_toolsets()
        catalog = get_catalog()

        publishing = {
            toolset_id
            for toolset_id in _TOOL_DOCS_REGISTRY
            # langchain publishes docs and is not a toolset; a manifest with no
            # tools_module has no signatures to check against.
            if (manifest := catalog.get(toolset_id)) is not None
            and manifest.tools_module
        }

        assert publishing == set(DOCUMENTED), (
            "DOCUMENTED is out of date — add the new toolset so its docs are "
            f"checked: {publishing ^ set(DOCUMENTED)}"
        )

    def test_every_rendered_doc_blob_is_registered(self) -> None:
        """A module that builds docs nothing serves is docs nobody reads.

        Five toolsets did exactly that — ``SLACK_TOOL_DOCS`` and four others
        were fully rendered at import and absent from the registry, so
        ``get_tool_docs("slack")`` answered step 3 of the agent's own process
        with an error while the answer sat one import away.
        """
        import pathlib
        import re

        from loom.agents.coding_tools import BUILTIN_TOOL_DOCS

        registered = {symbol for _, _, symbol in BUILTIN_TOOL_DOCS}
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "loom"

        defined: dict[str, str] = {}
        for path in root.rglob("tools.py"):
            for match in re.finditer(
                r"^([A-Z][A-Z0-9_]*_TOOL_DOCS)\s*[:=]", path.read_text(), re.M
            ):
                defined[match.group(1)] = str(path)

        missing = {
            symbol: path for symbol, path in defined.items() if symbol not in registered
        }

        assert not missing, (
            "these modules build tool docs that nothing registers — add them "
            f"to BUILTIN_TOOL_DOCS: {missing}"
        )


class TestEveryDocumentedSignatureIsReal:
    def test_the_parser_sees_something(self, documented) -> None:
        """A parser that matches nothing passes every check below."""
        toolset_id, docs, _ = documented

        assert _documented_signatures(docs), (
            f"{toolset_id}: no signature lines parsed — the checks below are blind"
        )

    def test_every_documented_function_exists(self, documented) -> None:
        toolset_id, docs, module = documented

        missing = [
            name
            for name in _documented_signatures(docs)
            if getattr(module, name, None) is None
        ]

        assert not missing, f"{toolset_id} documents functions that do not exist: {missing}"

    def test_every_documented_parameter_exists(self, documented) -> None:
        """The failure mode: the docs name a keyword and the model passes it.

        A parameter that is documented and absent is a TypeError on the first
        run, and one the repair loop cannot fix — the docs it would re-read to
        fix it are the thing that is wrong.
        """
        toolset_id, docs, module = documented
        wrong: list[str] = []

        for name, (params, _) in _documented_signatures(docs).items():
            fn = getattr(module, name, None)
            if fn is None:
                continue
            actual = list(inspect.signature(getattr(fn, "fn", fn)).parameters)
            for param in params:
                if param not in actual:
                    wrong.append(f"{name}({param}=...) — actual: {actual}")

        assert not wrong, (
            f"{toolset_id} documents parameters that do not exist:\n  "
            + "\n  ".join(wrong)
        )

    def test_no_documented_parameter_is_out_of_order(self, documented) -> None:
        """The examples call positionally, so order is part of the contract."""
        toolset_id, docs, module = documented
        wrong: list[str] = []

        for name, (params, _) in _documented_signatures(docs).items():
            fn = getattr(module, name, None)
            if fn is None:
                continue
            actual = list(inspect.signature(getattr(fn, "fn", fn)).parameters)
            documented_order = [p for p in params if p in actual]
            expected_order = [p for p in actual if p in documented_order]
            if documented_order != expected_order:
                wrong.append(f"{name}: docs say {documented_order}, code says {expected_order}")

        assert not wrong, (
            f"{toolset_id} documents parameters out of order:\n  "
            + "\n  ".join(wrong)
        )

    def test_the_documented_return_type_matches(self, documented) -> None:
        """``Results[T]`` versus ``list[T]`` is the coverage question — the docs
        claiming the wrong one is how a page gets reported as a total."""
        toolset_id, docs, module = documented
        wrong: list[str] = []

        for name, (_, returns) in _documented_signatures(docs).items():
            fn = getattr(module, name, None)
            if fn is None:
                continue
            actual = inspect.signature(getattr(fn, "fn", fn)).return_annotation
            actual = str(actual).replace("'", "").strip()
            if actual in ("", "inspect._empty"):
                continue
            if actual.split("[")[0] != returns.split("[")[0]:
                wrong.append(f"{name}: docs say {returns}, code returns {actual}")

        assert not wrong, f"{toolset_id} documents the wrong return type:\n  " + "\n  ".join(wrong)


class TestTheDocsCoverWhatIsThere:
    def test_no_operation_is_left_undocumented(self, documented) -> None:
        """An operation absent from the docs is an operation the agent will not
        use — it reads these before writing, not the manifest."""
        from loom.toolsets.registry import get_catalog

        toolset_id, docs, _ = documented
        manifest = get_catalog().get(toolset_id)
        assert manifest is not None

        undocumented = [
            op.function
            for op in manifest.all_operations()
            if op.function and op.function not in docs
        ]

        assert not undocumented, f"{toolset_id} docs omit: {undocumented}"


class TestNoImpossibleInstructions:
    def test_the_docs_do_not_promise_a_cursor(self, documented) -> None:
        """The regression this file was written for.

        No first-party read takes a ``cursor=`` argument: ``page_through``
        drives the loop inside the client to fill ``max_results``. The system
        prompt says so; the Jira docs said the opposite for long enough that
        it shipped.
        """
        toolset_id, docs, module = documented

        takes_cursor = any(
            "cursor" in inspect.signature(getattr(fn, "fn", fn)).parameters
            for name in _documented_signatures(docs)
            if (fn := getattr(module, name, None)) is not None
        )
        if takes_cursor:
            return

        offenders = [
            line.strip()
            for line in docs.splitlines()
            if re.search(r"(loop|paginate|iterate|continue|pass).{0,40}\bcursor\b", line, re.I)
        ]

        assert not offenders, (
            f"{toolset_id} docs describe looping with a cursor, but no tool "
            f"accepts one:\n  " + "\n  ".join(offenders)
        )


class TestEveryToolsetCanBeHandedToAnAgent:
    """Docs the agent can read are worth nothing if the tools cannot be built.

    ``gmail_send_message`` annotates a parameter ``list[Attachment] | None``
    and imported ``Attachment`` under ``TYPE_CHECKING``. These modules use
    postponed annotations, so pydantic saw the string and resolved it against
    the namespace of ``loom.agents.tools`` — which had never heard of it. The
    arguments model could not be built, and ``resolve_tools(["gmail"])`` raised
    rather than returning a short toolset: an agent handed Gmail failed before
    its first turn, and only for the toolsets whose signatures name a type.
    """

    def test_every_registered_toolset_resolves(self) -> None:
        from loom.toolsets.registry import get_catalog, register_available_toolsets

        register_available_toolsets()
        catalog = get_catalog()

        broken: list[str] = []
        for toolset_id in sorted(catalog.list_toolsets()):
            try:
                catalog.resolve_tools([toolset_id])
            except Exception as exc:
                broken.append(f"{toolset_id}: {type(exc).__name__}: {exc}")

        assert not broken, "toolsets an agent cannot be given:\n  " + "\n  ".join(broken)

    def test_a_tool_annotated_with_a_loom_type_keeps_its_schema(self) -> None:
        """Not merely "does not raise": the schema must describe the type."""
        from loom.agents.tools import build_parameter_schema
        from loom.toolsets.google.gmail.tools import gmail_send_message

        schema, _ = build_parameter_schema(gmail_send_message.fn)

        assert "Attachment" in schema.get("$defs", {})
