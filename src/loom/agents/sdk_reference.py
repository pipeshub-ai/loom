"""Looking up LOOM's own API, the way toolsets and nodes can already be looked up.

Every construct the coding agent is told to use has a tool behind it — except
the SDK itself. Toolsets have ``get_tool_docs`` and ``get_tool_contract``; nodes
have ``node_contract``, which exists because *"every schema→Python translation
is a chance to invent a keyword argument"*. ``@pure``, ``@effect``, ``ctx.now``
and the rest had nothing.

Observed, repeatedly: told to use ``@pure``, the agent searched the toolset
catalogue for it — ``search_operations("pure decorator loom import")``,
``get_tool_docs("slack")`` — because those were the only tools it had. Three to
five turns per run, spent asking a Slack integration how to write a LOOM
decorator, and the run then ended having written no ``@step`` at all, which the
validator flagged. Told to do something, unable to look it up, then reported for
not doing it.

**Rendered, never written down.** Every line comes from the object's own
signature, annotations and docstring, for the reason ``node_contract`` gives:
a second copy of a call's shape is one that can drift while the code cannot. So
there is no table of examples here to fall out of date — adding a keyword to
``@step`` changes what this returns, with no edit.

Two derivations do real work and both are read off the signature rather than
assumed:

* A **decorator** is a callable whose first parameter is positional-only, named
  ``fn``, and defaults to ``None``. That shape exists precisely so it can be
  used bare or called, and it is what lets this render both forms.
* Its target is ``async def`` when the annotation on ``fn`` mentions
  ``Awaitable``, and ``def`` when it does not. A workflow author who guesses
  wrong here gets a coroutine that is never awaited.
"""

from __future__ import annotations

import difflib
import inspect
from dataclasses import dataclass
from typing import Any

__all__ = ["SdkContract", "context_methods", "sdk_contract", "sdk_symbols"]


def sdk_symbols() -> tuple[str, ...]:
    """Every top-level name LOOM publishes.

    ``loom.__all__`` rather than a list kept here — it is the authoritative
    surface, so a symbol added to the package is looked-up-able the same day
    rather than when somebody remembers this file.
    """
    import loom

    return tuple(sorted(loom.__all__))


def context_methods() -> tuple[str, ...]:
    """Every ``ctx.*`` call a workflow body can make.

    The other half of what the agent writes, and the half a spec never spells
    out. Observed alongside the ``@pure`` hunt: ``search_operations("ctx.now
    durable clock current time deterministic")`` — the same dead end, one
    namespace over.
    """
    from loom.runtime.context import Context

    return tuple(sorted(
        name for name in dir(Context)
        if not name.startswith("_") and callable(getattr(Context, name, None))
    ))


@dataclass(frozen=True)
class SdkContract:
    """What to type, and what it means."""

    symbol: str
    kind: str
    """``decorator``, ``method``, ``class`` or ``value``."""
    import_line: str
    usage: str
    """The code to write. The substantive part — a signature describes a call,
    and the agent's next action is to *write* one."""
    summary: str
    options: tuple[str, ...] = ()
    """Keyword arguments, rendered with their defaults."""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "kind": self.kind,
            "import": self.import_line,
            "usage": self.usage,
            "summary": self.summary,
        }
        if self.options:
            payload["options"] = list(self.options)
        return payload


def sdk_contract(symbol: str) -> SdkContract:
    """The contract for one LOOM symbol or ``ctx.*`` method.

    Raises :class:`KeyError` naming near matches when nothing resolves, the
    behaviour ``CodeValidator`` already has for an import: a misspelling is the
    common case and the correction is cheap to offer.
    """
    name = symbol.strip().removeprefix("@")

    if name.startswith(("ctx.", "Context.")):
        return _context_contract(name.split(".", 1)[1])
    if name in context_methods() and name not in sdk_symbols():
        # `ctx.` omitted. Answering is better than refusing over a prefix the
        # agent had no reason to think mattered.
        return _context_contract(name)
    return _module_contract(name)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _module_contract(name: str) -> SdkContract:
    import loom

    if name not in sdk_symbols():
        raise KeyError(_unknown(name))
    obj = getattr(loom, name)
    summary = _summary(obj)
    import_line = f"from loom import {name}"

    target = _decorator_target(obj)
    if target is not None:
        options = _keyword_options(obj)
        usage = (
            f"@{name}\n"
            f"{target} my_{name}(...) -> ...:\n"
            f"    ..."
        )
        if options:
            usage += f"\n\n# with options:\n@{name}({options[0].split('=')[0]}=...)"
        return SdkContract(name, "decorator", import_line, usage, summary, options)

    if inspect.isclass(obj):
        try:
            signature = str(inspect.signature(obj))
        except (TypeError, ValueError):
            signature = "(...)"
        return SdkContract(
            name, "class", import_line, f"{name}{signature}", summary,
            _keyword_options(obj),
        )

    try:
        signature = str(inspect.signature(obj))
    except (TypeError, ValueError):
        return SdkContract(name, "value", import_line, name, summary)
    return SdkContract(
        name, "value", import_line, f"{name}{signature}", summary,
        _keyword_options(obj),
    )


def _context_contract(name: str) -> SdkContract:
    from loom.runtime.context import Context

    if name not in context_methods():
        raise KeyError(_unknown(name))
    method = getattr(Context, name)

    parameters = [
        p for key, p in inspect.signature(method).parameters.items() if key != "self"
    ]
    rendered = ", ".join(str(p) for p in parameters)
    call = f"ctx.{name}({rendered})"
    # `await` from the code, not from a list of which methods are async — one
    # more thing that would need maintaining beside the thing it describes.
    usage = f"await {call}" if inspect.iscoroutinefunction(method) else call
    return SdkContract(
        f"ctx.{name}", "method",
        "# on the `ctx` argument your workflow already receives",
        usage, _summary(method), _keyword_options(method),
    )


def _decorator_target(obj: Any) -> str | None:
    """``"async def"``, ``"def"``, or ``None`` when this is not a decorator.

    Read off the signature: a decorator usable bare *or* called takes its
    function as a positional-only first parameter defaulting to ``None``, which
    is the shape LOOM's step decorators all have. Nothing else in ``__all__``
    looks like that, so this identifies them without naming them.
    """
    if not inspect.isfunction(obj):
        return None
    try:
        parameters = list(inspect.signature(obj).parameters.values())
    except (TypeError, ValueError):
        return None
    if not parameters:
        return None
    first = parameters[0]
    if first.name != "fn" or first.kind is not inspect.Parameter.POSITIONAL_ONLY:
        return None
    if first.default is not None:
        return None
    return "async def" if _awaits(first.annotation, obj) else "def"


def _awaits(annotation: Any, owner: Any) -> bool:
    """Whether *annotation* describes a coroutine function.

    The annotation is usually a string — ``from __future__ import annotations``
    is on throughout — so this reads it, and follows an alias when it finds one.
    ``@workflow`` takes a ``WorkflowFn``, which says nothing on its face and
    resolves to ``Callable[..., Awaitable[Any]]``; without the second step a
    workflow body renders as ``def``, and a caller who copies that gets a
    coroutine nobody awaits.

    ``typing.get_type_hints`` is the obvious tool and is not usable here: it
    evaluates *every* annotation on the object, so one TYPE_CHECKING-only name
    anywhere in the signature raises and takes the whole lookup with it.
    Resolving the single name this needs, in the module that defined it, fails
    to a plain string instead.
    """
    text = str(annotation)
    if "Awaitable" in text or "Coroutine" in text:
        return True
    head = text.split("[", 1)[0].split("|", 1)[0].strip()
    if not head.isidentifier():
        return False
    resolved = getattr(owner, "__globals__", {}).get(head)
    return resolved is not None and (
        "Awaitable" in str(resolved) or "Coroutine" in str(resolved)
    )


def _keyword_options(obj: Any) -> tuple[str, ...]:
    """Keyword-only parameters, with their defaults, as they would be typed."""
    try:
        parameters = inspect.signature(obj).parameters
    except (TypeError, ValueError):
        return ()
    rendered = []
    for key, parameter in parameters.items():
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            continue
        if parameter.default is inspect.Parameter.empty:
            rendered.append(key)
        else:
            rendered.append(f"{key}={parameter.default!r}")
    return tuple(rendered)


def _summary(obj: Any) -> str:
    """The docstring's first paragraph — what the symbol is *for*.

    A paragraph rather than the whole thing: these docstrings are written for
    somebody reading the source, and the agent is paying for every line of it
    on the turn it asks.
    """
    doc = inspect.getdoc(obj) or ""
    paragraph: list[str] = []
    for line in doc.splitlines():
        if not line.strip() and paragraph:
            break
        if line.strip():
            paragraph.append(line.strip())
    return " ".join(paragraph)


def _unknown(name: str) -> str:
    known = [*sdk_symbols(), *(f"ctx.{m}" for m in context_methods())]
    close = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
    message = f"no LOOM symbol named {name!r}"
    if close:
        message += f". Did you mean {', '.join(repr(c) for c in close)}?"
    return message
