"""Guards that keep orchestration code deterministic.

Replay only works if re-running the workflow body issues the same sequence of durable
operations. The two most common ways to break that are reading a clock and reading a
random source directly in orchestration code. We defend on two fronts:

* a **static scan** of the workflow's source at declaration time, which catches the usual
  suspects immediately and points at the durable alternative;
* an optional **runtime guard** that patches the offending module-level functions and
  raises if they are called outside of a step body.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import random
import textwrap
import time
import uuid
import warnings
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loom.core.exceptions import DeterminismViolation

#: True while control is inside a step body, where non-determinism is expected and fine.
IN_STEP: ContextVar[bool] = ContextVar("workflow_in_step", default=False)

#: Dotted attribute paths that are unsafe in orchestration, mapped to their replacement.
UNSAFE_CALLS: dict[str, str] = {
    "datetime.now": "ctx.now()",
    "datetime.utcnow": "ctx.now()",
    "datetime.today": "ctx.now()",
    "date.today": "ctx.now().date()",
    "time.time": "ctx.now().timestamp()",
    "time.monotonic": "ctx.now().timestamp()",
    "time.sleep": "await ctx.sleep(...)",
    "asyncio.sleep": "await ctx.sleep(...)",
    # Not a clock or a random source, but the same class of defect: a durable
    # call's journal path is allocated when the call is constructed, so two
    # branches racing on one counter produce a numbering that follows timing
    # instead of the code. `ctx.gather` gives each branch its own numbering
    # space; these do not.
    "asyncio.gather": "await ctx.gather(...)",
    "asyncio.wait": "await ctx.gather(...)",
    "asyncio.as_completed": "await ctx.gather(...)",
    "asyncio.create_task": "await ctx.gather(...)",
    "asyncio.ensure_future": "await ctx.gather(...)",
    "asyncio.TaskGroup": "await ctx.gather(...)",
    "uuid.uuid1": "ctx.uuid4()",
    "uuid.uuid4": "ctx.uuid4()",
    "random.random": "ctx.random().random()",
    "random.randint": "ctx.random().randint(...)",
    "random.choice": "ctx.random().choice(...)",
    "random.shuffle": "ctx.random().shuffle(...)",
    "random.uniform": "ctx.random().uniform(...)",
    "secrets.token_hex": "ctx.uuid4().hex",
    "os.urandom": "ctx.random().randbytes(...)",
}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One non-determinism finding in a workflow body."""

    symbol: str
    replacement: str
    lineno: int

    def __str__(self) -> str:
        return (
            f"line {self.lineno}: '{self.symbol}()' is non-deterministic in orchestration "
            f"code; use {self.replacement} so the value is journaled and replays identically"
        )


def scan_for_nondeterminism(fn: Callable[..., Any]) -> list[Diagnostic]:
    """Statically inspect a workflow body for direct non-deterministic calls.

    Best effort: returns an empty list when source is unavailable. Calls nested inside a
    ``@step`` are not visible here because steps are separate functions, which is exactly
    the separation we want to encourage.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, IndentationError):
        return []

    findings: list[Diagnostic] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted is None:
            continue
        for symbol, replacement in UNSAFE_CALLS.items():
            if dotted == symbol or dotted.endswith("." + symbol):
                findings.append(Diagnostic(symbol, replacement, node.lineno))
                break
    return findings


def _dotted_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif parts:
        return None
    else:
        return None
    return ".".join(reversed(parts))


def warn_if_nondeterministic(fn: Callable[..., Any], *, workflow_name: str) -> list[Diagnostic]:
    """Emit a warning for each finding; returns them so callers can escalate to an error."""
    findings = scan_for_nondeterminism(fn)
    for finding in findings:
        warnings.warn(
            f"workflow '{workflow_name}' {finding}",
            DeterminismWarning,
            stacklevel=3,
        )
    return findings


class DeterminismWarning(UserWarning):
    """Warned when a workflow body reads a non-deterministic source directly."""


@contextlib.contextmanager
def step_scope() -> Iterator[None]:
    """Mark the enclosing block as step execution, where non-determinism is permitted."""
    token = IN_STEP.set(True)
    try:
        yield
    finally:
        IN_STEP.reset(token)


@contextlib.contextmanager
def strict_determinism() -> Iterator[None]:
    """Patch the common non-deterministic entry points for the duration of the block.

    Opt-in, and intended for tests and CI rather than production: patching is process
    global while active, though the :data:`IN_STEP` context variable keeps step bodies
    (which run in their own asyncio tasks, and therefore their own context) unaffected.
    """
    originals: dict[tuple[Any, str], Any] = {}

    def guard(module: Any, attr: str, hint: str) -> None:
        original = getattr(module, attr)
        originals[(module, attr)] = original

        def patched(*args: Any, **kwargs: Any) -> Any:
            if not IN_STEP.get():
                raise DeterminismViolation(
                    f"{module.__name__}.{attr}() was called from orchestration code; use {hint}"
                )
            return original(*args, **kwargs)

        setattr(module, attr, patched)

    guard(time, "time", "ctx.now().timestamp()")
    guard(time, "monotonic", "ctx.now().timestamp()")
    guard(uuid, "uuid4", "ctx.uuid4()")
    guard(random, "random", "ctx.random().random()")
    guard(random, "randint", "ctx.random().randint(...)")
    guard(random, "choice", "ctx.random().choice(...)")

    try:
        yield
    finally:
        for (module, attr), original in originals.items():
            setattr(module, attr, original)
