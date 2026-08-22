"""What a generated workflow will do to the world, before it is run.

`loom author` wrote a file and stopped, so *"find my jira tickets"* — a
question with an answer — produced a Python file and the sentence
`loom run my_jira_tickets`. The task was the query; the answer is the tickets.

Running it is the missing step, and running code a model wrote a second ago,
against real credentials, is not something to do blind. This is what the
decision is made from: every durable call the file makes, and the effect class
its *manifest* declares — never the model's own account of it. A self-report
would certify exactly the case the check exists to catch, which is the position
`IdentifierStage` already takes about resolved ids.

**Undeclared is an effect, not a read.** `OperationSpec.effect` defaults to
WRITE for the same reason: the failure mode of guessing "read" is a refund
issued without anybody being asked, and the failure mode of guessing "write" is
one keystroke. A step that genuinely reaches nothing says so with `@pure`,
which is a declaration the author makes rather than an inference this module
draws from the shape of a function body.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["DurableCall", "Impact", "Verdict", "impact_of"]


class Verdict(StrEnum):
    """Whether this file can be run without asking first."""

    READ_ONLY = "read_only"
    """Every durable call is a declared read, or reaches nothing."""

    EFFECTFUL = "effectful"
    """Something writes, deletes, or is not classified at all."""

    EMPTY = "empty"
    """No durable calls — nothing to weigh, and nothing to run either."""


@dataclass(frozen=True)
class DurableCall:
    """One `ctx.*` call the file makes, and what it was declared to be."""

    kind: str
    """`step`, `node`, `agent`, `approval`, or another `ctx` method."""
    target: str
    """The step function, the node id, or `""` where the call names nothing."""
    effect: str = ""
    """`read` / `write` / `destructive`, or `""` when nothing declares it."""
    line: int = 0

    @property
    def reads_only(self) -> bool:
        return self.effect == "read"


@dataclass(frozen=True)
class Impact:
    """The verdict, and the calls it was reached from."""

    verdict: Verdict
    calls: tuple[DurableCall, ...] = ()

    @property
    def writes(self) -> tuple[DurableCall, ...]:
        """What made it effectful — the lines a person should be shown.

        Naming them is the whole value: "this writes" is not something anybody
        can act on, and "this calls jira_transition_issue" is.
        """
        return tuple(call for call in self.calls if not call.reads_only)

    @property
    def safe_to_run(self) -> bool:
        return self.verdict is Verdict.READ_ONLY


#: `ctx` methods that reach the outside world and are therefore weighed.
#:
#: Deliberately not every method in `DURABLE_CTX_CALLS`: `ctx.sleep`,
#: `ctx.state` and `ctx.report` are durable and reach nobody, and counting them
#: would make every workflow effectful — which is the same as counting none.
_WEIGHED = {"step", "node", "agent", "call", "child"}

#: Methods that park the run on a person. Not an effect: an approval is the
#: thing that *asks*, and treating it as a write would mean a workflow with a
#: human gate needs a second one to reach the first.
_ASKS = {"wait_for_approval", "wait_for_human", "ask"}


def impact_of(source: str, *, toolsets: Any = None, nodes: Any = None) -> Impact:
    """Weigh *source* against what the registries declare.

    *toolsets* answers `effect_of(function_name)` — the manifest's declaration
    for a toolset operation, which is where every classification that matters
    comes from. *nodes* answers `show(node_id)`. Both optional: with neither,
    every call is unclassified and the verdict is `EFFECTFUL`, which is the
    fail-safe direction.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A file that will not parse cannot be run, and refusing to guess is
        # better than reporting it harmless.
        return Impact(Verdict.EFFECTFUL)

    pure = _pure_steps(tree)
    declared = _declared_effects(tree, toolsets)
    declared.update(_wrappers(tree, declared))
    calls: list[DurableCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = _ctx_method(node.func)
        if method is None:
            continue
        if method in _ASKS:
            calls.append(DurableCall("approval", _first_string(node), "read", node.lineno))
            continue
        if method not in _WEIGHED:
            continue
        calls.append(_weigh(method, node, pure, declared, nodes))

    if not calls:
        return Impact(Verdict.EMPTY)
    verdict = (
        Verdict.READ_ONLY
        if all(call.reads_only for call in calls)
        else Verdict.EFFECTFUL
    )
    return Impact(verdict, tuple(calls))


def _weigh(
    method: str, node: ast.Call, pure: set[str], declared: dict[str, str], nodes: Any
) -> DurableCall:
    if method == "agent":
        # A model call reaches a provider and returns text. It is not a write
        # to anything the *spec* named, and treating it as one would make every
        # judgement step need a confirmation.
        return DurableCall("agent", "", "read", node.lineno)

    target = _target(method, node)
    if method == "node":
        return DurableCall("node", target, _node_effect(target, nodes), node.lineno)

    if target in pure:
        # `@pure` is the author's declaration that this reaches nothing. The
        # alternative is inferring it from the function body, which is the kind
        # of guess this module exists to avoid.
        return DurableCall(method, target, "read", node.lineno)

    return DurableCall(method, target, declared.get(target, ""), node.lineno)


def _declared_effects(tree: ast.AST, toolsets: Any) -> dict[str, str]:
    """`function name -> effect`, for every toolset this file imports from.

    Built from the file's own imports rather than asked of
    `ToolsetRegistry.effect_of`, and the difference is scope. `effect_of` is
    the broker's per-dispatch lookup and is deliberately *execution* scope — it
    answers only for toolsets somebody registered, because widening it would
    reclassify steps in deployments that registered none, and that changes what
    a run is allowed to do.

    This is not a policy decision, it is a declaration being read back to a
    person before they press a key. So it reads the **catalogue**: the file
    says `from loom.toolsets.jira.tools import jira_search_issues`, which names
    the toolset exactly, and the manifest says what that operation is.
    """
    if toolsets is None:
        return {}
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    ids = getattr(toolsets, "catalogue_ids", None)
    try:
        listed = list(ids()) if callable(ids) else list(toolsets.toolset_ids)
    except Exception:
        return {}

    effects: dict[str, str] = {}
    for toolset_id in listed:
        manifest = toolsets.get(toolset_id)
        module = getattr(manifest, "tools_module", "")
        if not module or not any(
            imported == module or imported.startswith(module + ".")
            for imported in modules
        ):
            continue
        for operation in manifest.all_operations():
            if operation.function:
                effects[operation.function] = str(
                    getattr(operation.effect, "value", operation.effect)
                )
    return effects


def _node_effect(node_id: str, nodes: Any) -> str:
    show = getattr(nodes, "show", None)
    if not node_id or show is None:
        return ""
    try:
        detail = show(node_id)
    except Exception:
        return ""
    effect = getattr(detail, "effect", "")
    return str(getattr(effect, "value", effect) or "")


def _wrappers(tree: ast.AST, declared: dict[str, str]) -> dict[str, str]:
    """Local `@step`s that only call classified toolset operations.

    The agent almost always writes a thin wrapper — `async def resolve_project`
    that calls `jira_resolve_project` inside — because the prompt tells it to
    put I/O inside a step. So the *durable call* names a local function and
    every generated workflow came out "unclassified", which asks about a
    workflow whose whole content is two reads.

    One level, and only when **every** toolset call inside is declared and they
    agree. A wrapper that calls a read and a write is the write, and one that
    calls something nothing declares stays unclassified — the fail-safe
    direction, and the reason this is not general inference: it answers "does
    this function do exactly one already-declared thing", which is a question
    with a certain answer, rather than "what does this function do".
    """
    resolved: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if not any(
            _decorator_name(d) in ("step", "effect", "node") for d in node.decorator_list
        ):
            continue
        called = {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        effects = {declared[name] for name in called if name in declared}
        unknown = called & _suspicious(called, declared)
        if len(effects) == 1 and not unknown:
            resolved[node.name] = next(iter(effects))
    return resolved


def _suspicious(called: set[str], declared: dict[str, str]) -> set[str]:
    """Names that look like toolset calls but carry no declaration.

    A wrapper calling `len()` or `str()` is not doing undeclared I/O, and
    treating it as such would leave every wrapper unclassified — which is the
    same as not having this at all. Only names that are neither builtins nor
    declared count against it.
    """
    import builtins

    return {
        name
        for name in called
        if name not in declared and not hasattr(builtins, name)
    }


def _pure_steps(tree: ast.AST) -> set[str]:
    """Functions this file declares with `@pure`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if _decorator_name(decorator) == "pure":
                found.add(node.name)
    return found


def _decorator_name(decorator: ast.expr) -> str:
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    if isinstance(decorator, ast.Name):
        return decorator.id
    return ""


def _ctx_method(func: ast.expr) -> str | None:
    """`ctx.step` -> `"step"`, anything else -> `None`.

    Matches the receiver by name rather than by type, which is what the graph
    extractor does: a workflow body's first parameter is `ctx` by convention
    and by every example in the prompt.
    """
    if not isinstance(func, ast.Attribute):
        return None
    value = func.value
    if isinstance(value, ast.Name) and value.id == "ctx":
        return func.attr
    # `ctx.nested(...).step(...)` and friends.
    if isinstance(value, ast.Call):
        inner = _ctx_method(value.func)
        if inner is not None:
            return func.attr
    return None


def _target(method: str, node: ast.Call) -> str:
    if not node.args:
        return ""
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.Name):
        return first.id
    if isinstance(first, ast.Attribute):
        return first.attr
    return ""


def _first_string(node: ast.Call) -> str:
    for argument in node.args:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
    return ""
