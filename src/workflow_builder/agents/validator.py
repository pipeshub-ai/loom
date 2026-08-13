"""Code validator for generated workflow code.

Performs AST-based checks for common issues in model-generated
workflows: syntax errors, missing decorators, bare I/O in workflow
bodies, nondeterministic API usage, and missing imports.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass

#: Always importable regardless of the configured allowlist.
ALWAYS_ALLOWED: frozenset[str] = frozenset({"workflow_builder"})

#: Store constructors. Generated workflow modules should not call these at
#: import time — persistence belongs to whoever deploys the workflow.
STORE_CONSTRUCTORS: frozenset[str] = frozenset(
    {"MemoryStore", "SQLiteStore", "PostgresStore", "MongoStore"}
)

#: Modules safe to import while validating, for checking that a symbol exists.
#: Deliberately narrow — importing an arbitrary package runs its side effects.
RESOLVABLE_PREFIXES: frozenset[str] = frozenset({"workflow_builder"})

BARE_IO_CALLS: set[str] = {
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "httpx.get",
    "httpx.post",
    "open",
    "aiohttp.request",
}

NONDETERMINISTIC_CALLS: set[str] = {
    "datetime.now",
    "uuid.uuid4",
    "uuid4",
    "random.random",
    "random.randint",
    "random.choice",
}


@dataclass
class CodeIssue:
    """A single issue found in generated workflow code."""

    category: str  # "syntax" | "structure" | "determinism" | "imports"
    message: str
    severity: str  # "error" | "warning"


class CodeValidator:
    """Validate model-generated workflow code via AST analysis.

    Parameters
    ----------
    allowed_packages:
        Third-party distributions the generated code may import. The standard
        library and ``workflow_builder`` are always permitted. Leave as ``None``
        to skip the check entirely — pass a set when you know what is installed
        in the environment the workflow will run in, so the agent finds out at
        validation time rather than at import time on someone else's machine.
    """

    def __init__(
        self,
        allowed_packages: Iterable[str] | None = None,
        *,
        available_toolsets: Iterable[str] | None = None,
        toolset_modules: dict[str, str] | None = None,
    ) -> None:
        self.allowed_packages = None if allowed_packages is None else set(allowed_packages)
        self.available_toolsets = (
            None if available_toolsets is None else set(available_toolsets)
        )
        self.toolset_modules = toolset_modules or {}
        """Toolset id to its real importable module.

        A toolset's id and its module are not the same string — ``google_calendar``
        lives at ``workflow_builder.toolsets.google.calendar`` — and a model that
        knows only the id builds the import from it. That resolves as a plausible
        path, passes an id-based check, and fails at import."""
        """Toolsets this environment actually has. ``None`` disables the check.

        A spec that needs Slack, generated against an environment with no Slack
        toolset, produces code importing a module that is not installed — and
        the model writes it confidently, because inventing an integration reads
        like completing the task. Naming what is available turns that into a
        refusal the caller can act on."""

    def _check_toolsets(self, tree: ast.AST) -> list[CodeIssue]:
        """Reject imports of toolsets this environment does not have."""
        if self.available_toolsets is None and not self.toolset_modules:
            return []

        issues: list[CodeIssue] = []
        seen: set[str] = set()
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name if node.names else ""
            if not module.startswith("workflow_builder.toolsets."):
                continue

            # Compare against the real module paths where they are known: a
            # toolset id is not a module name, and checking the id lets an
            # invented path through.
            if self.toolset_modules:
                if any(
                    module == known or module.startswith(known + ".")
                    for known in self.toolset_modules.values()
                ):
                    continue
                if module in seen:
                    continue
                seen.add(module)
                issues.append(
                    CodeIssue(
                        "toolset",
                        f"no module {module!r}. Import a toolset by the exact "
                        "path its documentation gives, not one built from its "
                        "id. Available: "
                        + ", ".join(sorted(self.toolset_modules.values())),
                        "error",
                    )
                )
                continue

            parts = module.split(".")
            if len(parts) < 3:
                continue
            # google toolsets nest one deeper: ...toolsets.google.gmail.tools
            toolset = parts[3] if parts[2] == "google" and len(parts) > 3 else parts[2]
            if toolset in self.available_toolsets or toolset in seen:
                continue
            seen.add(toolset)
            issues.append(
                CodeIssue(
                    "toolset",
                    f"this environment has no {toolset!r} toolset "
                    f"(available: {', '.join(sorted(self.available_toolsets)) or 'none'}). "
                    "Do not write code against an integration that is not "
                    "configured — say the task cannot be done here instead.",
                    "error",
                )
            )
        return issues

    def validate(self, code: str) -> list[CodeIssue]:
        """Run all checks and return discovered issues.

        Args:
            code: Python source code to validate.

        Returns:
            List of issues found, possibly empty.
        """
        issues: list[CodeIssue] = []

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            msg = f"Syntax error: {exc.msg} (line {exc.lineno})"
            return [CodeIssue("syntax", msg, "error")]

        issues.extend(self._check_toolsets(tree))

        self._check_structure(tree, issues)
        self._check_imports(code, issues)
        self._check_allowed_packages(tree, issues)
        self._check_symbols(tree, issues)
        self._check_no_store_choice(tree, issues)
        return issues

    # ------------------------------------------------------------------
    # Structure checks
    # ------------------------------------------------------------------

    def _check_structure(
        self,
        tree: ast.Module,
        issues: list[CodeIssue],
    ) -> None:
        has_workflow = False
        has_step = False

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and self._is_workflow_func(node):
                has_workflow = True
                self._check_workflow_body(node, issues)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for dec in node.decorator_list:
                    name = self._decorator_name(dec)
                    if name in {"step", "pure", "effect"}:
                        has_step = True

        if not has_workflow:
            issues.append(
                CodeIssue(
                    "structure",
                    "No @workflow-decorated async function found",
                    "error",
                ),
            )
        # A workflow built entirely from toolset operations declares no step of
        # its own — those are steps already, and the prompt says to call them
        # directly. Warning about it would nag every integration workflow.
        uses_toolset_steps = any(
            isinstance(n, ast.ImportFrom)
            and (n.module or "").startswith("workflow_builder.toolsets.")
            for n in ast.walk(tree)
        )
        if not has_step and not uses_toolset_steps:
            issues.append(
                CodeIssue(
                    "structure",
                    "No @step/@pure/@effect function found",
                    "warning",
                ),
            )

    def _check_workflow_body(
        self,
        node: ast.AsyncFunctionDef,
        issues: list[CodeIssue],
    ) -> None:
        """Flag bare I/O and nondeterminism inside a workflow body."""
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = self._call_name(child)
            if not name:
                continue
            if name in BARE_IO_CALLS:
                issues.append(
                    CodeIssue(
                        "structure",
                        f"Bare I/O call '{name}' in workflow body"
                        " — wrap in a @step",
                        "error",
                    ),
                )
            if name in NONDETERMINISTIC_CALLS:
                issues.append(
                    CodeIssue(
                        "determinism",
                        f"Nondeterministic call '{name}' in"
                        " workflow body — use ctx equivalent",
                        "error",
                    ),
                )

    def _is_workflow_func(
        self,
        node: ast.AsyncFunctionDef,
    ) -> bool:
        """True when *node* carries a ``@workflow`` or ``@flow`` decorator."""
        return any(
            self._decorator_name(d) in {"workflow", "flow"}
            for d in node.decorator_list
        )

    # ------------------------------------------------------------------
    # Import checks
    # ------------------------------------------------------------------

    def _check_imports(
        self,
        code: str,
        issues: list[CodeIssue],
    ) -> None:
        if "workflow_builder" not in code:
            issues.append(
                CodeIssue(
                    "imports",
                    "Missing workflow_builder import",
                    "error",
                ),
            )

    def _check_allowed_packages(
        self,
        tree: ast.Module,
        issues: list[CodeIssue],
    ) -> None:
        """Flag imports of packages the target environment does not have."""
        if self.allowed_packages is None:
            return

        permitted = self.allowed_packages | ALWAYS_ALLOWED | sys.stdlib_module_names
        for root in sorted(_imported_roots(tree)):
            if root in permitted or root.startswith("_"):
                continue
            issues.append(
                CodeIssue(
                    "imports",
                    f"Import of '{root}' is not available in the target "
                    f"environment. Allowed third-party packages: "
                    f"{', '.join(sorted(self.allowed_packages)) or 'none'}.",
                    "error",
                ),
            )

    def _check_no_store_choice(
        self, tree: ast.Module, issues: list[CodeIssue]
    ) -> None:
        """Flag a workflow module that picks its persistence *at import time*.

        Where the journal lives is a deployment decision, so a module that binds
        one the moment it is imported cannot be pointed at Postgres without
        editing it.

        Only module-scope construction counts. Inside a function — a ``main()``,
        a fixture, a factory — the choice is made by whoever calls it, which is
        exactly the right place for it.
        """
        for node in _module_scope_calls(tree):
            name = self._call_name(node).split(".")[-1]
            if name in STORE_CONSTRUCTORS:
                issues.append(
                    CodeIssue(
                        "structure",
                        f"'{name}()' is constructed at import time. A workflow "
                        "module should not bind its own store — take one from the "
                        "caller, or use Runtime.from_env() inside a main().",
                        "warning",
                    ),
                )

    def _check_symbols(self, tree: ast.Module, issues: list[CodeIssue]) -> None:
        """Verify ``from X import Y`` actually finds ``Y`` in ``X``.

        The allowlist check above only sees package *names*, so a real package
        with a misspelled symbol — ``from workflow_builder import Retryy`` — sails
        through and fails at import time on the user's machine instead.

        Only modules on :data:`RESOLVABLE_PREFIXES` are imported. Importing an
        arbitrary third-party package to check a name would run its side effects
        during what is supposed to be a static check.
        """
        import importlib

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 0:
                continue
            module = node.module or ""
            if not _is_resolvable(module):
                continue
            try:
                imported = importlib.import_module(module)
            except Exception:
                continue

            for alias in node.names:
                if alias.name == "*" or hasattr(imported, alias.name):
                    continue
                # A submodule is importable without being an attribute yet.
                try:
                    importlib.import_module(f"{module}.{alias.name}")
                except Exception:
                    issues.append(
                        CodeIssue(
                            "imports",
                            f"'{module}' has no attribute '{alias.name}'"
                            + _suggest(imported, alias.name),
                            "error",
                        ),
                    )

    # ------------------------------------------------------------------
    # AST helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _decorator_name(decorator: ast.expr) -> str:
        """Extract the bare name from a decorator node."""
        if isinstance(decorator, ast.Name):
            return decorator.id
        if isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Name):
                return decorator.func.id
            if isinstance(decorator.func, ast.Attribute):
                return decorator.func.attr
        if isinstance(decorator, ast.Attribute):
            return decorator.attr
        return ""

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        """Extract a dotted call name like ``requests.get``."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            parts: list[str] = [node.func.attr]
            value = node.func.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
            return ".".join(reversed(parts))
        return ""


def _imported_roots(tree: ast.Module) -> set[str]:
    """Every top-level package name imported anywhere in *tree*.

    Walks the whole module, so an import tucked inside a step body counts too —
    that is the usual place a model puts one.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        # `from . import x` is relative, so there is no package to attribute it to.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _is_resolvable(module: str) -> bool:
    """Whether importing *module* during validation is safe."""
    root = module.split(".")[0]
    return root in RESOLVABLE_PREFIXES or root in sys.stdlib_module_names


def _suggest(module: object, wanted: str) -> str:
    """Offer the closest public name, so the fix is obvious from the message."""
    import difflib

    candidates = [name for name in dir(module) if not name.startswith("_")]
    close = difflib.get_close_matches(wanted, candidates, n=1, cutoff=0.7)
    return f"; did you mean '{close[0]}'?" if close else ""


def _is_main_guard(node: ast.stmt) -> bool:
    """True for ``if __name__ == "__main__":`` — the one block import skips."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def _module_scope_calls(tree: ast.Module) -> list[ast.Call]:
    """Calls that execute when the module is imported.

    Descends into module-level control flow (``if``, ``try``, ``with``), since
    that still runs on import. Stops at two places that do not: any function or
    class body, and the ``__main__`` guard.
    """
    found: list[ast.Call] = []

    def walk(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) or _is_main_guard(node):
                continue
            found.extend(
                child
                for statement in ast.iter_child_nodes(node)
                if not isinstance(statement, ast.stmt)
                for child in ast.walk(statement)
                if isinstance(child, ast.Call)
            )
            for attr in ("body", "orelse", "finalbody"):
                walk([s for s in getattr(node, attr, []) if isinstance(s, ast.stmt)])
            for handler in getattr(node, "handlers", []):
                walk(handler.body)

    walk(tree.body)
    return found
