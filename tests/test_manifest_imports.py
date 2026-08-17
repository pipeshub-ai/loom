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

from loom.toolsets.asana.manifest import ASANA_MANIFEST
from loom.toolsets.clickup.manifest import CLICKUP_MANIFEST
from loom.toolsets.confluence.manifest import CONFLUENCE_MANIFEST
from loom.toolsets.duckduckgo.manifest import DUCKDUCKGO_MANIFEST
from loom.toolsets.exa.manifest import EXA_MANIFEST
from loom.toolsets.github.manifest import GITHUB_MANIFEST
from loom.toolsets.gitlab.manifest import GITLAB_MANIFEST
from loom.toolsets.google import (
    GMAIL_MANIFEST,
    GOOGLE_CALENDAR_MANIFEST,
    GOOGLE_DRIVE_MANIFEST,
    GOOGLE_MEET_MANIFEST,
)
from loom.toolsets.hubspot.manifest import HUBSPOT_MANIFEST
from loom.toolsets.jira.manifest import JIRA_MANIFEST
from loom.toolsets.microsoft.onedrive.manifest import ONEDRIVE_MANIFEST
from loom.toolsets.microsoft.onenote.manifest import ONENOTE_MANIFEST
from loom.toolsets.microsoft.outlook.calendar.manifest import (
    OUTLOOK_CALENDAR_MANIFEST,
)
from loom.toolsets.microsoft.outlook.mail.manifest import OUTLOOK_MAIL_MANIFEST
from loom.toolsets.microsoft.sharepoint.manifest import SHAREPOINT_MANIFEST
from loom.toolsets.microsoft.teams.manifest import TEAMS_MANIFEST
from loom.toolsets.salesforce.manifest import SALESFORCE_MANIFEST
from loom.toolsets.slack.manifest import SLACK_MANIFEST
from loom.toolsets.tavily.manifest import TAVILY_MANIFEST
from loom.toolsets.zoom.manifest import ZOOM_MANIFEST

FIRST_PARTY = [
    GMAIL_MANIFEST,
    GOOGLE_CALENDAR_MANIFEST,
    GOOGLE_DRIVE_MANIFEST,
    GOOGLE_MEET_MANIFEST,
    JIRA_MANIFEST,
    CONFLUENCE_MANIFEST,
    CLICKUP_MANIFEST,
    ASANA_MANIFEST,
    SALESFORCE_MANIFEST,
    HUBSPOT_MANIFEST,
    GITHUB_MANIFEST,
    GITLAB_MANIFEST,
    ONEDRIVE_MANIFEST,
    SHAREPOINT_MANIFEST,
    TEAMS_MANIFEST,
    ONENOTE_MANIFEST,
    OUTLOOK_MAIL_MANIFEST,
    OUTLOOK_CALENDAR_MANIFEST,
    SLACK_MANIFEST,
    ZOOM_MANIFEST,
    EXA_MANIFEST,
    TAVILY_MANIFEST,
    DUCKDUCKGO_MANIFEST,
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


@pytest.mark.parametrize("manifest", FIRST_PARTY, ids=lambda m: m.id)
class TestDeclaredOutputSchemasAreWellFormed:
    """What every manifest's ``_array`` helper exists to produce.

    Each toolset builds its ``output_schema`` from a Pydantic model, through a
    two-line local helper. The helper was annotated ``model: type`` — wide
    enough to accept any class at all, while calling ``model_json_schema()``,
    a Pydantic classmethod, on it. Passing the wrong thing was therefore an
    ``AttributeError`` raised from inside a manifest at *import* time, which is
    the moment the toolset registry reads it: the toolset does not fail to
    describe itself, it fails to exist.

    Typing it ``type[BaseModel]`` moves that to the type checker. This checks
    the property from the other end — that what comes out is a JSON Schema the
    coding agent and the fake generator can actually use — because the
    annotation only constrains the callers mypy can see.
    """

    def test_every_declared_schema_is_a_json_schema(self, manifest) -> None:
        bad = [
            f"{op.id}: {op.output_schema!r}"
            for op in manifest.all_operations()
            if op.output_schema and "type" not in op.output_schema
        ]
        assert not bad, f"{manifest.id} declares a schema with no type:\n  " + "\n  ".join(bad)

    def test_an_array_schema_says_what_it_is_an_array_of(self, manifest) -> None:
        """The half a bare ``{"type": "array"}`` loses.

        ``Results[T]`` and ``list[T]`` are where the fake generator learns what
        a row looks like; an array with no ``items`` generates fakes of nothing
        and the smoke test proves correspondingly little.
        """
        bad = []
        for op in manifest.all_operations():
            schema = op.output_schema
            if schema.get("type") != "array":
                continue
            items = schema.get("items")
            if not isinstance(items, dict) or not (
                items.get("type") or items.get("properties") or items.get("$ref")
            ):
                bad.append(f"{op.id}: items={items!r}")
        assert not bad, f"{manifest.id} declares a shapeless array:\n  " + "\n  ".join(bad)

    def test_a_typed_return_is_not_declared_as_a_shapeless_object(
        self, manifest
    ) -> None:
        """``{"type": "object"}`` is honest for a genuinely free-form return.

        ``salesforce_describe_object`` hands back an org's own field metadata
        and ``zoom_get_past_meeting`` whatever Zoom sends; both are annotated
        ``dict``, and inventing properties for them would be worse than
        declaring none. So the check is not "objects must have properties" —
        it is that an operation whose tool returns a *Pydantic model* must say
        so, because there the schema was available and got dropped.
        """
        import importlib
        import typing

        from pydantic import BaseModel

        module = importlib.import_module(manifest.tools_module)
        bad = []
        for op in manifest.all_operations():
            schema = op.output_schema
            if schema.get("type") != "object" or schema.get("properties"):
                continue
            fn = getattr(module, op.function, None) if op.function else None
            if fn is None:
                continue
            try:
                returns = typing.get_type_hints(getattr(fn, "fn", fn)).get("return")
            except Exception:
                continue
            if isinstance(returns, type) and issubclass(returns, BaseModel):
                bad.append(f"{op.id}: {op.function} returns {returns.__name__}")
        assert not bad, (
            f"{manifest.id} declares a shapeless object for a typed return:\n  "
            + "\n  ".join(bad)
        )


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
        """The same list the rest of this file checks.

        It used to be a second copy, and the copies drifted the moment a
        toolset was added: the new one appeared in FIRST_PARTY, passed every
        import check, and was silently skipped by the paging checks here —
        which are the ones that found six real drifts. One list, so adding a
        toolset cannot half-enrol it.
        """
        return FIRST_PARTY

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
        "google_drive": "loom.toolsets.google.drive.client",
        "google_meet": "loom.toolsets.google.meet.client",
        "clickup": "loom.toolsets.clickup.client",
        "asana": "loom.toolsets.asana.client",
        "salesforce": "loom.toolsets.salesforce.client",
        "hubspot": "loom.toolsets.hubspot.client",
        "github": "loom.toolsets.github.client",
        "gitlab": "loom.toolsets.gitlab.client",
        "onedrive": "loom.toolsets.microsoft.onedrive.client",
        "sharepoint": "loom.toolsets.microsoft.sharepoint.client",
        "teams": "loom.toolsets.microsoft.teams.client",
        "onenote": "loom.toolsets.microsoft.onenote.client",
        "outlook_mail": "loom.toolsets.microsoft.outlook.mail.client",
        "outlook_calendar": "loom.toolsets.microsoft.outlook.calendar.client",
        "slack": "loom.toolsets.slack.client",
        "zoom": "loom.toolsets.zoom.client",
        "exa": "loom.toolsets.exa.client",
        "tavily": "loom.toolsets.tavily.client",
        "duckduckgo": "loom.toolsets.duckduckgo.client",
    }

    #: Every way a client in this repo drives a paging loop.
    #:
    #: `page_through(` was missing, and it is the idiom every non-Google client
    #: uses — so this returned an empty set for jira, confluence, clickup,
    #: asana, salesforce, hubspot, github and gitlab, and the check below
    #: `continue`d past every one of their operations. Eight toolsets whose
    #: coverage-drift guard silently checked nothing, including two that were
    #: drifting at the time.
    PAGING_CALLS = ("collect(", "paginate(", "page_through(", ".mapped(", ".filtered(")

    def _client_pages(self, module_name: str) -> set[str]:
        """Client methods that run a paging loop.

        Source inspection, and deliberately so: the client is the only place
        that knows the truth, and asking it by *calling* it would need
        credentials.
        """
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        sources: dict[str, str] = {}
        for _, klass in inspect.getmembers(module, inspect.isclass):
            if not klass.__module__.startswith(module_name):
                continue
            for name, method in inspect.getmembers(klass, inspect.isfunction):
                try:
                    sources[name] = inspect.getsource(method)
                except OSError:
                    continue

        found = {
            name
            for name, source in sources.items()
            if any(call in source for call in self.PAGING_CALLS)
        }
        # Transitively: a method that calls a paging method pages too. Without
        # this the detector stops at the first hop, and Salesforce's
        # `find_accounts` -> `_query_rows` -> `query` reads as not paging — one
        # level of indirection was enough to hide coverage being computed and
        # then discarded by a comprehension.
        changed = True
        while changed:
            changed = False
            for name, source in sources.items():
                if name in found:
                    continue
                if any(f"self.{callee}(" in source for callee in found):
                    found.add(name)
                    changed = True
        return found

    def test_the_paging_detector_recognises_every_client_idiom(self) -> None:
        """The detector is the guard's eyesight, so its blind spots are silent.

        Asserted per client rather than in aggregate: one toolset going dark is
        invisible in a total, and going dark is exactly the failure — the check
        below skips an operation whose client method it did not recognise, so a
        detector that sees nothing passes everything.
        """
        # Exa and Tavily genuinely do not page — neither API has a cursor of
        # any kind, which is why their reads return plain lists and declare
        # `pagination=False`. Their emptiness here is a fact about the vendors,
        # not a blind spot.
        no_cursor_upstream = {"exa", "tavily"}
        blind = [
            toolset
            for toolset, module in self.CLIENTS.items()
            if toolset not in no_cursor_upstream and not self._client_pages(module)
        ]

        assert not blind, f"paging detector sees nothing for: {blind}"

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
                # Only a read that hands back *rows* owes a coverage answer. A
                # resolver returns one thing or nothing — `calendar_find_calendar`,
                # `jira_resolve_user`, `salesforce_whoami` — and "did I see
                # everything?" is not a question about a single object, however
                # much paging went into finding it.
                # Read through the @step wrapper the way `paginates` does — its
                # own __annotations__ describe the decorator, not the tool — and
                # as text, because these modules use postponed annotations.
                import inspect as _inspect

                target = getattr(fn, "fn", fn)
                try:
                    returns = str(_inspect.signature(target).return_annotation)
                except (TypeError, ValueError):
                    continue
                if not returns.startswith("list["):
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


class TestNoToolShadowsCtxStepsOwnArguments:
    """A tool parameter that ``ctx.step`` already claims cannot be passed.

    ``Context.step(target, /, *args, name=…, retry=…, timeout=…, on_error=…,
    fallback=…)`` takes those names for itself, so a tool declaring one of them
    is unreachable by keyword: ``ctx.step(create_task, name="Fix login")`` binds
    ``name`` to the *step name override* and the tool is then called with the
    argument missing. The failure is a TypeError deep inside the step, which
    reads as a broken tool rather than as a shadowed keyword.

    Positional calls still work, which is exactly why this survives review.
    """

    #: Reserved by ``ctx.step`` itself.
    SHADOWED: ClassVar[frozenset[str]] = frozenset(
        {"name", "retry", "timeout", "on_error", "fallback"}
    )

    #: Offenders that predate this check, named rather than excluded silently.
    #:
    #: Each of these is genuinely unreachable by keyword today. Renaming them
    #: changes a published signature, so it is a decision to take deliberately
    #: rather than as a drive-by fix — and Meet's ``name`` is the API's own
    #: resource name (``spaces/abc``), so the rename there is not obvious
    #: either. Listing them keeps the gap visible: an empty exclusion list and
    #: a green test would claim coverage this does not have.
    KNOWN: ClassVar[frozenset[str]] = frozenset(
        {
            # gmail
            "gmail_create_label",
            "gmail_rename_label",
            # google_drive
            "drive_copy_file",
            "drive_create_folder",
            "drive_find_folder",
            "drive_rename_file",
            "drive_upload_file",
            # google_meet — `name` is the API's own resource name (spaces/abc),
            # so even the rename is not obvious here.
            "meet_end_active_conference",
            "meet_get_conference_record",
            "meet_get_space",
            "meet_update_space",
            # jira
            "jira_resolve_user",
        }
    )

    def test_no_first_party_tool_declares_a_shadowed_parameter(self) -> None:
        import importlib
        import inspect

        offenders: list[str] = []
        for manifest in FIRST_PARTY:
            module = importlib.import_module(manifest.tools_module)
            for op in manifest.all_operations():
                fn = getattr(module, op.function, None) if op.function else None
                if fn is None or op.function in self.KNOWN:
                    continue
                target = getattr(fn, "fn", fn)
                shadowed = self.SHADOWED & set(inspect.signature(target).parameters)
                if shadowed:
                    offenders.append(f"{op.function}: {sorted(shadowed)}")

        assert not offenders, (
            "these tools declare a parameter ctx.step reserves, so it cannot "
            "be passed by keyword:\n  " + "\n  ".join(offenders)
        )
