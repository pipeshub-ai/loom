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
ALWAYS_ALLOWED: frozenset[str] = frozenset({"loom"})

#: Store constructors. Generated workflow modules should not call these at
#: import time — persistence belongs to whoever deploys the workflow.
STORE_CONSTRUCTORS: frozenset[str] = frozenset(
    {"MemoryStore", "SQLiteStore", "PostgresStore", "MongoStore"}
)

#: Modules safe to import while validating, for checking that a symbol exists.
#: Deliberately narrow — importing an arbitrary package runs its side effects.
RESOLVABLE_PREFIXES: frozenset[str] = frozenset({"loom"})

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

#: Calls that execute text as code, reach the host, or manipulate the process.
#:
#: This validator was a determinism-and-structure linter, and the premise of the
#: whole coding-agent design is that the generated code is untrusted. The sandbox
#: contains *execution*; refusing at authoring time is the cheaper layer and the
#: one that produces a message somebody can act on — a workflow that shells out
#: is a review finding, not a runtime error to be discovered on a Tuesday.
#:
#: Named calls only. This is a tripwire against the obvious, not a proof: an
#: attacker with `getattr` and string building is not stopped by a name list,
#: and pretending otherwise is worse than being explicit that the sandbox is
#: what actually holds.
DANGEROUS_CALLS: set[str] = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "importlib.import_module",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.fork",
    "os.kill",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "shutil.rmtree",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.Popen",
    "socket.socket",
    "socket.create_connection",
    "ctypes.CDLL",
    "pickle.loads",
    "marshal.loads",
    "setattr",
}

#: Modules whose mere import in generated workflow code is worth a look.
#: Warned, not refused: `subprocess` inside a `@step` may be exactly the job.
DANGEROUS_IMPORTS: set[str] = {
    "ctypes",
    "marshal",
    "multiprocessing",
    "pty",
    "socket",
    "subprocess",
}

NONDETERMINISTIC_CALLS: set[str] = {
    "datetime.now",
    "uuid.uuid4",
    "uuid4",
    "random.random",
    "random.randint",
    "random.choice",
}

#: Concurrency primitives that schedule work the journal cannot identify, and
#: the ``ctx`` equivalent that can.
#:
#: A durable call takes its journal path from a counter, allocated when the
#: call is *constructed*. Under a raw ``asyncio.gather`` two branches allocate
#: from the same counter as they run, so the numbering depends on how long the
#: previous step took — and on replay, where the timings differ, two logically
#: distinct call sites are served each other's recorded values. ``ctx.gather``
#: gives each branch its own numbering space and does not have this problem,
#: which is why this is a redirection rather than a prohibition.
UNSAFE_CONCURRENCY: dict[str, str] = {
    "asyncio.gather": "ctx.gather(...)",
    "asyncio.wait": "ctx.gather(...)",
    "asyncio.as_completed": "ctx.gather(...)",
    "asyncio.create_task": "ctx.gather(...)",
    "asyncio.ensure_future": "ctx.gather(...)",
    "asyncio.TaskGroup": "ctx.gather(...)",
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
        library and ``loom`` are always permitted. Leave as ``None``
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
        lives at ``loom.toolsets.google.calendar`` — and a model that
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
            if not module.startswith("loom.toolsets."):
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
            # Only reachable with no declared modules — every branch above
            # continues when there are any — and the guard at the top returned
            # when *that* coincided with no declared toolset set. So this is
            # non-None here, by an invariant that spans two conditions and is
            # therefore invisible to a type checker. Bound and tested rather
            # than asserted, so the unreachable case degrades to "no opinion"
            # instead of an assertion failure in front of a user.
            available = self.available_toolsets
            if available is None or toolset in available or toolset in seen:
                continue
            seen.add(toolset)
            issues.append(
                CodeIssue(
                    "toolset",
                    f"this environment has no {toolset!r} toolset "
                    f"(available: {', '.join(sorted(available)) or 'none'}). "
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
        self._check_dangerous(tree, issues)
        return issues

    def _check_dangerous(self, tree: ast.Module, issues: list[CodeIssue]) -> None:
        """Flag code execution, process control, and host access.

        Scoped to ``@workflow`` and ``@step`` bodies — the code that will run
        durably against real credentials — rather than the whole module. A
        generated file also carries a ``if __name__ == "__main__"`` demo block,
        and the surrounding cookbook and host scripts legitimately shell out to
        *run* a generated workflow and clean up a temp file afterwards. Judging
        those by the same rule flags the harness for doing its job, which is the
        same precedent ``_check_no_store_choice`` already follows: constructing
        a store is wrong in the library and fine in the script.

        Both bodies, not just the orchestration one: unlike determinism, which
        is a property of the workflow body alone, this is about what runs — and
        a ``subprocess.run`` inside a ``@step`` is exactly as much of a decision.

        Errors for executing text as code or shelling out, because there is no
        version of those a spec asks for. Warnings for the imports, because
        ``socket`` or ``subprocess`` in a step can be the job.

        A name list is a tripwire, not a proof. Someone composing ``getattr``
        and a string gets past it, and the sandbox is what actually holds — this
        is the cheap layer that turns the obvious cases into a review finding
        instead of a runtime surprise.
        """
        flagged_imports: set[str] = set()
        for node in self._durable_nodes(tree):
            if isinstance(node, ast.Call):
                name = self._call_name(node)
                if name and name in DANGEROUS_CALLS:
                    issues.append(
                        CodeIssue(
                            "security",
                            f"'{name}' executes code or reaches the host directly. "
                            "A generated workflow should express what it needs "
                            "through a toolset operation or a @step, not by "
                            "running arbitrary code.",
                            "error",
                        )
                    )
                continue
            root = ""
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in DANGEROUS_IMPORTS and root not in flagged_imports:
                        flagged_imports.add(root)
                        issues.append(self._risky_import(root))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in DANGEROUS_IMPORTS and root not in flagged_imports:
                    flagged_imports.add(root)
                    issues.append(self._risky_import(root))

    def _durable_nodes(self, tree: ast.Module) -> list[ast.AST]:
        """Every node inside a ``@workflow`` or ``@step`` body, plus its imports.

        Module-level imports are included because that is where a step's
        ``import subprocess`` actually sits — the call is in the body and the
        import is at the top, and reporting one without the other names half the
        decision.
        """
        found: list[ast.AST] = [
            node
            for node in tree.body
            if isinstance(node, ast.Import | ast.ImportFrom)
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            names = {self._decorator_name(d) for d in node.decorator_list}
            if not names & {"workflow", "flow", "step", "pure", "effect", "node"}:
                continue
            for child in node.body:
                found.extend(ast.walk(child))
        return found

    @staticmethod
    def _risky_import(root: str) -> CodeIssue:
        return CodeIssue(
            "security",
            f"imports '{root}', which reaches outside the workflow. Legitimate "
            "inside a @step for some jobs — worth a second look before this "
            "runs against real credentials.",
            "warning",
        )

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
            and (n.module or "").startswith("loom.toolsets.")
            for n in ast.walk(tree)
        )
        # And the same holds for a body whose durable work *is* a node or an
        # agent call: both are journaled units already, so there is no step
        # left to write. "tell me a joke" is one `ctx.agent`, and telling its
        # author to add a @step names work that does not exist — the nag the
        # exemption above exists to avoid, one call shape over.
        uses_durable_units = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("node", "agent")
            for n in ast.walk(tree)
        )
        if not has_step and not uses_toolset_steps and not uses_durable_units:
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
        """Flag bare I/O, nondeterminism, and control-signal-swallowing
        `except` clauses inside a workflow body."""
        for child in ast.walk(node):
            if isinstance(child, ast.ExceptHandler):
                self._check_except_handler(child, issues)
                continue
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
            if name in UNSAFE_CONCURRENCY:
                issues.append(
                    CodeIssue(
                        "determinism",
                        f"'{name}' in a workflow body schedules durable calls "
                        f"the journal cannot tell apart on replay — two "
                        f"branches allocate journal paths from one counter as "
                        f"they run, so the numbering follows timing rather "
                        f"than the code. Use "
                        f"{UNSAFE_CONCURRENCY[name]}, which gives each branch "
                        f"its own numbering space.",
                        "error",
                    ),
                )

    def _check_except_handler(
        self,
        handler: ast.ExceptHandler,
        issues: list[CodeIssue],
    ) -> None:
        """`Suspend`/`WorkflowCancelled`/`ContinueAsNew` derive from
        `BaseException` specifically so `except Exception` cannot swallow
        them (see `core/exceptions.py`'s module docstring) -- but a bare
        `except:` or an explicit `except BaseException:` catches
        `BaseException` too. Inside a workflow body that clause sits between
        the `ctx.*` call that raises the signal and the engine that is
        supposed to see it propagate, so `ctx.wait_for_approval()` silently
        never parks, `ctx.sleep()` silently never suspends, and a cancelled
        run keeps running. This is the one case `except Exception` does not
        already cover, so it is checked separately rather than folded into
        `BARE_IO_CALLS`/`NONDETERMINISTIC_CALLS`, which key off call names,
        not handler types.
        """
        if handler.type is None:
            issues.append(
                CodeIssue(
                    "structure",
                    "Bare 'except:' in workflow body catches BaseException, "
                    "which silently swallows Suspend/WorkflowCancelled and "
                    "strands the run instead of parking or cancelling it — "
                    "catch a specific exception type instead",
                    "error",
                ),
            )
            return
        caught_names = self._exception_type_names(handler.type)
        if "BaseException" in caught_names:
            issues.append(
                CodeIssue(
                    "structure",
                    "'except BaseException' in workflow body silently "
                    "swallows Suspend/WorkflowCancelled and strands the run "
                    "instead of parking or cancelling it — catch a specific "
                    "exception type instead",
                    "error",
                ),
            )

    @staticmethod
    def _exception_type_names(node: ast.expr) -> set[str]:
        """Bare names in an `except X:`/`except (X, Y):` clause. Does not
        resolve aliases or subclassing — a workflow that imports
        `BaseException` under another name is rare enough that a false
        negative here is an acceptable trade for not needing an import
        resolver in a syntax-level check."""
        if isinstance(node, ast.Tuple):
            names: set[str] = set()
            for elt in node.elts:
                names |= CodeValidator._exception_type_names(elt)
            return names
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Attribute):
            return {node.attr}
        return set()

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
        if "loom" not in code:
            issues.append(
                CodeIssue(
                    "imports",
                    "Missing loom import",
                    "error",
                ),
            )

    def _check_allowed_packages(
        self,
        tree: ast.Module,
        issues: list[CodeIssue],
    ) -> None:
        """Flag imports of packages the target environment does not have.

        A toolset's own ``tools_module`` is permitted here even when its root
        package is outside ``allowed_packages`` — a custom (non-``loom``)
        toolset that follows "Manifests must say how to import themselves"
        (``ToolsetManifest.import_line()``) necessarily lives at some other
        root, e.g. a host application's own package. Without this, the
        narrowest and therefore most common ``allowed_packages`` setting for
        generated code — "nothing beyond ``loom``" — would make every such
        toolset's declared import unusable: the model writes exactly the
        import the manifest told it to, and this check fails it anyway. The
        match is against the toolset's *exact* declared module path, or a
        submodule of it, never its bare root, so declaring one toolset's
        import path does not accidentally widen what else that root package
        exposes.
        """
        if self.allowed_packages is None:
            return

        permitted_roots = self.allowed_packages | ALWAYS_ALLOWED | sys.stdlib_module_names
        permitted_modules = set(self.toolset_modules.values())
        flagged_roots: set[str] = set()
        for module, root in sorted(_imported_modules(tree)):
            if root in permitted_roots or root.startswith("_") or root in flagged_roots:
                continue
            if any(
                module == known or module.startswith(known + ".")
                for known in permitted_modules
            ):
                continue
            flagged_roots.add(root)
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
        with a misspelled symbol — ``from loom import Retryy`` — sails
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
    return {root for _module, root in _imported_modules(tree)}


def _imported_modules(tree: ast.Module) -> set[tuple[str, str]]:
    """Every ``(module_path, root_package)`` pair imported anywhere in *tree*.

    The root alone is what ``allowed_packages`` is checked against; the full
    path is what an exact ``toolset_modules`` entry is checked against — a
    toolset's declared module is a specific path, not merely "some import
    under this root package".
    """
    modules: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update((alias.name, alias.name.split(".")[0]) for alias in node.names)
        # `from . import x` is relative, so there is no package to attribute it to.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add((node.module, node.module.split(".")[0]))
    return modules


def _is_resolvable(module: str) -> bool:
    """Whether importing *module* during validation is safe."""
    root = module.split(".")[0]
    return root in RESOLVABLE_PREFIXES or root in sys.stdlib_module_names


#: Packages LOOM deliberately keeps out of its top-level namespace, in the
#: order a wrongly-placed name is looked for. ``loom.nodes`` and
#: ``loom.toolsets`` are excluded from ``loom.__all__`` on purpose — ``Node``
#: beside the ``@node`` step decorator would put two unrelated things one
#: autocomplete apart — and triggers were never re-exported either. So "right
#: name, wrong module" is not a typo here, it is the *designed* shape, and it
#: is the failure a model hits by writing the import the rest of the prompt
#: taught it.
_SIBLING_NAMESPACES = ("loom.triggers", "loom.nodes", "loom.toolsets")


def _suggest(module: object, wanted: str) -> str:
    """Say how to fix the import, not merely that it is broken.

    Two different mistakes, and only one of them was covered. A *misspelling*
    is answered by the closest public name in the same module. A name that is
    spelled correctly and lives somewhere else was answered with
    ``'loom' has no attribute 'After'`` and nothing more — true, and unusable:
    the repair loop reads the message, has no move to make, and spends both its
    attempts rewriting an import that was one module away from correct.

    The second case is not an edge case here. ``loom.__all__`` is a curated 36
    symbols and every trigger sits outside it, so a model told to write
    ``triggers=[After(minutes=2)]`` — with every other symbol in the prompt
    coming from ``loom`` — writes ``from loom import After`` and is wrong for a
    reason no message told it.
    """
    import difflib
    import importlib

    candidates = [name for name in dir(module) if not name.startswith("_")]
    close = difflib.get_close_matches(wanted, candidates, n=1, cutoff=0.7)
    if close:
        return f"; did you mean '{close[0]}'?"

    name = getattr(module, "__name__", "")
    if name.split(".")[0] != "loom":
        return ""
    for sibling in _SIBLING_NAMESPACES:
        if sibling == name:
            continue
        try:
            found = importlib.import_module(sibling)
        except Exception:
            continue
        if hasattr(found, wanted):
            return f"; it lives in {sibling} — from {sibling} import {wanted}"
    return ""


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
