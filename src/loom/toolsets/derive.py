"""Recover each operation's HTTP verb from its client, without importing it.

The verb is the strongest signal available about what an operation does to the
world: measured across the shipped toolsets, it agrees with the declared
:class:`~loom.toolsets.manifest.EffectClass` in 89 of 91 recoverable cases, and
both exceptions are the same shape — a search issued as a ``POST``.

**Static analysis, not import.** ``loom certify`` is meant to be cheap enough
for a pre-commit hook, and importing a client pulls in httpx and the vendor's
models. More importantly it would break Layer 1: the catalog and grant
validation read manifest metadata without importing a toolset, and a derivation
that needed the module would make "what does this operation do?" cost an import
of every integration installed.

**Coverage is partial and is reported rather than hidden.** A client method that
issues two different verbs has no single answer, and a service where everything
is a ``POST`` — Slack — carries no signal at all. An operation with no
recoverable verb simply yields nothing here; :func:`~loom.toolsets.effects.
derive_effect_profile` falls through to the scopes and then to the fail-safe
default. Claiming coverage that does not exist would be worse than having none,
because a green check would then mean "nothing was checked".
"""

from __future__ import annotations

import ast
from pathlib import Path

__all__ = [
    "VERB_LITERALS",
    "verbs_for_manifest",
    "verbs_in_client",
    "wiring_in_tools",
]

VERB_LITERALS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})

#: Methods a client routes its requests through. Four shapes because four
#: toolsets were written before there was a convention; all of them take the
#: verb as the first positional argument.
_REQUEST_METHODS = frozenset({"_request", "request", "_call", "_data", "_get", "_post"})


def _first_verb_literal(node: ast.AST) -> str | None:
    """The HTTP verb in ``self._request("GET", ...)``, if this call is one."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else None
    if name not in _REQUEST_METHODS:
        return None
    for arg in node.args:
        if isinstance(arg, ast.Constant) and arg.value in VERB_LITERALS:
            return str(arg.value)
    for kw in node.keywords:
        if (
            kw.arg in ("method", "verb")
            and isinstance(kw.value, ast.Constant)
            and kw.value.value in VERB_LITERALS
        ):
            return str(kw.value.value)
    return None


def verbs_in_client(source: str) -> dict[str, str]:
    """``{client method: HTTP verb}`` for methods issuing exactly one verb.

    Exactly one, deliberately. A method that issues a ``GET`` and then a
    ``DELETE`` is a destructive method whose class cannot be read off a single
    literal, and picking either one is how ``drive_trash_file`` would come back
    a read.
    """
    tree = ast.parse(source)
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name in _REQUEST_METHODS:
            continue  # the helper itself, not a caller
        verbs = {
            verb
            for child in ast.walk(node)
            if (verb := _first_verb_literal(child)) is not None
        }
        if verbs:
            found.setdefault(node.name, set()).update(verbs)
    return {name: next(iter(v)) for name, v in found.items() if len(v) == 1}


def wiring_in_tools(source: str) -> dict[str, str]:
    """``{tool function: client method it delegates to}``.

    Every shipped tool is a thin wrapper — ``return await
    get_default_client().send_message(...)`` — so the client method is the one
    attribute call in its body that is not a local helper. A tool calling more
    than one distinct client method is skipped for the same reason a
    two-verb client method is.
    """
    tree = ast.parse(source)
    wiring: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        calls = {
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and not child.func.attr.startswith("_")
        }
        if calls:
            wiring.setdefault(node.name, set()).update(calls)
    return {fn: next(iter(c)) for fn, c in wiring.items() if len(c) == 1}


def verbs_for_manifest(manifest_path: Path) -> dict[str, str]:
    """``{tool function: HTTP verb}`` for one toolset, by reading its files.

    Looks for ``client.py`` and ``tools.py`` beside the manifest, joins the two
    maps, and returns only the operations where both halves resolved.
    """
    directory = Path(manifest_path).parent
    client = directory / "client.py"
    tools = directory / "tools.py"
    if not client.is_file() or not tools.is_file():
        return {}
    try:
        methods = verbs_in_client(client.read_text(encoding="utf-8"))
        wiring = wiring_in_tools(tools.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    return {
        fn: methods[method] for fn, method in wiring.items() if method in methods
    }
