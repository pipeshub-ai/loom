"""Rules an example must follow, checked over its AST.

An example is not just code that works — it is the code people copy. A
reference workflow that reaches for ``httpx`` teaches everyone who reads it to
bypass the toolset layer, and with it the error classification, the pagination,
the effect classes, and the fakes that make a workflow testable. That is why
these rules live here and not in ``CodeValidator``: a hand-written workflow may
legitimately call ``httpx``, while an example whose whole job is to show the
intended shape may not.

Three rules, each the enforcement of one property from
``docs/design/reference-workflows-plan.md``:

``no-bare-http``
    Every external call goes through a toolset step or a node.

``no-credential-arguments``
    Credentials resolve from the environment or a ``ConnectionBroker``. Passing
    one as a workflow input or a step argument writes it to the journal —
    ``loom show`` prints it back — and nothing in the journal path redacts.

``bounded-fan-out``
    ``ctx.gather`` over a comprehension issues one call per item at once.
    ``ctx.map(..., max_concurrency=…)`` and ``control.throttle`` exist.

**The allowlist is the point.** All ten reference workflows break the first two
today; phase 2 of the plan rewrites them onto toolsets and deletes their
entries. Landing the rules with the current violations recorded is what makes
that progress visible, and what stops an eleventh example from arriving with the
same defects. :func:`test_allowlist_has_no_stale_entries` fails when an entry is
no longer needed, so the list shrinks and cannot quietly outlive its reason.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

SUPPORT = {"__init__.py", "utils.py", "conftest.py", "mock_http.py"}
EXCLUDED_DIRS = {"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

#: HTTP clients an example must not reach for directly.
HTTP_MODULES = {"httpx", "requests", "aiohttp", "urllib3", "urllib.request"}

#: Names that mean "this value is a secret". Matched whole-word against a
#: parameter or model field name.
CREDENTIAL = re.compile(
    r"(^|_)(api_key|apikey|token|secret|password|passwd|credential|credentials"
    r"|access_key|private_key|client_secret|signing_secret)($|_)"
)

RULES = ("no-bare-http", "no-credential-arguments", "bounded-fan-out")

#: Which examples each rule applies to, as a path prefix under ``examples/``.
#:
#: ``no-bare-http`` is scoped to the reference workflows on purpose. Their
#: subject *is* the integration, so reaching past the toolset layer is the
#: defect. A cookbook example demonstrating engine mechanics is a different
#: thing — ``16_http_server.py`` uses ``httpx`` to call the server it just
#: started, and ``01``/``02`` fetch a public JSON API because the lesson is
#: step chaining, not the API. Holding those to the integration rule would
#: force a toolset into an example that is not about one.
#:
#: The other two are properties of any workflow, so they apply everywhere.
SCOPE: dict[str, str] = {
    "no-bare-http": "reference",
    "no-credential-arguments": "",
    "bounded-fan-out": "",
}

#: ``{path relative to the repo: {rule it is known to break}}``.
#:
#: Every entry is a defect recorded in the audit, not an exemption granted.
#: Phase 2 of the plan removes them file by file as each workflow is rewritten
#: onto toolsets, nodes, triggers, and human gates.
ALLOWED: dict[str, set[str]] = {
    # The canonical fan-out example, and the unbounded form is what it
    # demonstrates. Left as a recorded violation rather than capped, because
    # adding `max_concurrency` to the example whose subject is "true
    # parallelism" would blunt the lesson to satisfy a rule aimed elsewhere.
    "examples/cookbook/02_parallel.py": {"bounded-fan-out"},
}


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    rule: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.detail}"


def _decorator_names(node: ast.AST) -> set[str]:
    """Every dotted decorator name on a function, e.g. ``{"step", "workflow"}``."""
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        while isinstance(target, ast.Attribute):
            target = target.value
        if isinstance(target, ast.Name):
            names.add(target.id)
    return names


def bare_http(tree: ast.AST) -> list[Violation]:
    """An HTTP client imported directly, rather than a toolset or a node."""
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in HTTP_MODULES:
                    found.append(
                        Violation("no-bare-http", node.lineno, f"import {alias.name}")
                    )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] in HTTP_MODULES
        ):
            found.append(
                Violation("no-bare-http", node.lineno, f"from {node.module} import …")
            )
    return found


def credential_arguments(tree: ast.AST) -> list[Violation]:
    """A secret named as a step parameter or an input model's field.

    Reading one from the environment inside a step body is fine and is not
    matched — what this catches is a secret that becomes a *journaled* value by
    travelling as an argument.
    """
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if not _decorator_names(node) & {"step", "workflow", "pure", "effect"}:
                continue
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if CREDENTIAL.search(arg.arg):
                    found.append(
                        Violation(
                            "no-credential-arguments",
                            arg.lineno,
                            f"{node.name}({arg.arg}=…) — a step argument is journaled",
                        )
                    )
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and CREDENTIAL.search(stmt.target.id)
                ):
                    found.append(
                        Violation(
                            "no-credential-arguments",
                            stmt.lineno,
                            f"{node.name}.{stmt.target.id} — an input field is "
                            f"journaled",
                        )
                    )
    return found


BUILT_COLLECTION = (ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.List)


def _collections_built_in(scope: ast.AST) -> set[str]:
    """Names bound to a list built in this function, by comprehension or literal.

    Spreading such a name into ``gather`` is the same defect as spreading the
    comprehension directly — ``tasks = [ctx.step(...) for x in xs]`` followed by
    ``ctx.gather(*tasks)`` issues exactly as many concurrent calls. Two of the
    reference workflows use the indirect form, so a detector that only matched
    the inline one would have reported them clean.
    """
    names: set[str] = set()
    for node in ast.walk(scope):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if not isinstance(node.value, BUILT_COLLECTION):
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def unbounded_fan_out(tree: ast.AST) -> list[Violation]:
    """``ctx.gather`` over a per-item collection with no concurrency cap."""
    found: list[Violation] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        built = _collections_built_in(scope)
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "gather"):
                continue
            if any(kw.arg == "max_concurrency" for kw in node.keywords):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Starred):
                    continue
                spread = arg.value
                if isinstance(spread, BUILT_COLLECTION):
                    shape = "*[… for … in …]"
                elif isinstance(spread, ast.Name) and spread.id in built:
                    shape = f"*{spread.id}"
                else:
                    continue
                found.append(
                    Violation(
                        "bounded-fan-out",
                        node.lineno,
                        f"ctx.gather({shape}) with no max_concurrency — one "
                        f"concurrent call per item",
                    )
                )
    return found


DETECTORS = {
    "no-bare-http": bare_http,
    "no-credential-arguments": credential_arguments,
    "bounded-fan-out": unbounded_fan_out,
}


def example_files() -> list[Path]:
    """Every example the rules apply to."""
    return [
        path
        for path in sorted(EXAMPLES.rglob("*.py"))
        if path.name not in SUPPORT and not EXCLUDED_DIRS.intersection(path.parts)
    ]


def violations_in(path: Path, rule: str) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    return DETECTORS[rule](tree)


def in_scope(path: Path, rule: str) -> bool:
    """Whether *rule* applies to *path*."""
    prefix = SCOPE[rule]
    return not prefix or path.relative_to(EXAMPLES).parts[0] == prefix


FILES = example_files()
CHECKS = [(path, rule) for path in FILES for rule in RULES if in_scope(path, rule)]
CHECK_IDS = [f"{p.relative_to(EXAMPLES)}::{r}" for p, r in CHECKS]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_there_are_examples_to_check() -> None:
    """A rule suite over an empty file list passes and means nothing."""
    assert len(FILES) >= 30, f"only found {len(FILES)} examples"


@pytest.mark.parametrize(("path", "rule"), CHECKS, ids=CHECK_IDS)
def test_example_follows_rule(path: Path, rule: str) -> None:
    relative = str(path.relative_to(ROOT))
    if rule in ALLOWED.get(relative, set()):
        pytest.skip(f"{relative} is a known violator of {rule} — see the plan")

    found = violations_in(path, rule)
    assert not found, f"{relative} breaks {rule}:\n  " + "\n  ".join(
        str(v) for v in found
    )


@pytest.mark.parametrize(
    ("relative", "rule"),
    [(rel, rule) for rel, rules in ALLOWED.items() for rule in sorted(rules)],
    ids=[
        f"{rel.split('/')[-1]}::{rule}"
        for rel, rules in ALLOWED.items()
        for rule in sorted(rules)
    ],
)
def test_allowlist_has_no_stale_entries(relative: str, rule: str) -> None:
    """An entry that is no longer needed must be deleted, not left behind.

    Without this the list only ever grows, and a file repaired in phase 2 goes
    on being exempt from the rule it now satisfies — which is how an allowlist
    stops describing anything.
    """
    path = ROOT / relative
    assert path.exists(), f"{relative} is allowlisted but does not exist"
    assert violations_in(path, rule), (
        f"{relative} no longer breaks {rule} — remove it from ALLOWED"
    )


def test_allowlist_names_only_known_rules() -> None:
    unknown = {r for rules in ALLOWED.values() for r in rules} - set(RULES)
    assert not unknown, f"ALLOWED names rules that do not exist: {sorted(unknown)}"
