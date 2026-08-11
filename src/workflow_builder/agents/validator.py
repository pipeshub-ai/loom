"""Code validator for generated workflow code.

Performs AST-based checks for common issues in model-generated
workflows: syntax errors, missing decorators, bare I/O in workflow
bodies, nondeterministic API usage, and missing imports.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

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
    """Validate model-generated workflow code via AST analysis."""

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

        self._check_structure(tree, issues)
        self._check_imports(code, issues)
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
        if not has_step:
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
