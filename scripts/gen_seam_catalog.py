"""Generate a reference page per swappable seam, and fail CI when it drifts.

LOOM already does this in three narrow places — ``loom check --fail-on-change``
for graphs and descriptions, ``test_manifest_imports`` executing every declared
import line, and the ``docs-examples`` CI job running every README snippet. Each
one exists because a document that describes code eventually stops describing
it, and nobody finds out from reading.

The same instinct, pointed at the ports. A seam's page is generated from the
Protocol itself: its methods, their signatures, and their docstrings, plus the
implementations found in the tree and the modules that consume them. Run it
with ``--check`` and it exits non-zero when a page no longer matches the code.

    python scripts/gen_seam_catalog.py           # write the pages
    python scripts/gen_seam_catalog.py --check   # CI: fail if stale

Deliberately nine seams and one gate, not a doc-sync suite. The value is the
alarm, not the document — the first time a Protocol gains a method and the
catalog fails before review does, it has paid for itself.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "loom"
DOCS = ROOT / "docs" / "seams"

#: Protocol name → (module path relative to the package, one-line purpose).
#:
#: Curated rather than discovered: a Protocol is not automatically a seam. What
#: earns a page is having at least two implementations and a consumer that
#: names neither — which is the property that makes swapping one a
#: configuration change rather than a fork.
SEAMS: dict[str, tuple[str, str]] = {
    "ExecutionStore": ("stores/base.py", "Where runs and journals are persisted"),
    "StateStore": ("runtime/state.py", "The KV space shared by every run of a workflow"),
    "Clock": ("runtime/clock.py", "Every timestamp and every wait the engine takes"),
    "BlobBackend": ("blobs/blob.py", "Content-addressed storage for oversized values"),
    "ModelProvider": ("agents/models.py", "One model vendor behind one method"),
    "AgentBackend": ("agents/backend.py", "Which agent framework runs ctx.agent()"),
    "QueueBackend": ("triggers/queue.py", "Where at-least-once messages come from"),
    "SpillStore": ("agents/bounds.py", "Where an oversized tool result is kept"),
    "EffectBroker": ("runtime/effects.py", "What every durable operation is weighed against"),
    "ExecutionSandbox": ("runtime/sandbox.py", "Where a workflow body is invoked"),
    "EventLog": ("events/log.py", "The durable, resumable record of what the world said"),
    "Checkpoints": ("events/log.py", "Where each subscriber has read to"),
    "EventSource": ("events/sources.py", "Whether a delivery is really from the provider"),
    "Reconciler": (
        "events/reconcile.py",
        "Turning a provider pointer into the events it stands for",
    ),
    "Watch": ("events/watch.py", "A provider-side subscription that expires"),
}

MARKER_BEGIN = "<!-- BEGIN GENERATED — do not edit below this line -->"
MARKER_END = "<!-- END GENERATED -->"


@dataclass(frozen=True)
class Seam:
    """One port, as the source describes it."""

    name: str
    module: str
    purpose: str
    doc: str
    methods: list[tuple[str, str]]
    implementations: list[str]
    consumers: list[str]

    def render(self) -> str:
        lines = [
            f"# {self.name}",
            "",
            f"*{self.purpose}.*",
            "",
            f"Defined in `{self.module}`.",
            "",
            MARKER_BEGIN,
            "",
        ]
        if self.doc:
            lines += [self.doc.strip(), ""]

        lines += ["## Contract", ""]
        for signature, doc in self.methods:
            lines.append(f"### `{signature}`")
            lines.append("")
            if doc:
                lines.append(doc.strip().split("\n\n")[0])
                lines.append("")

        lines += ["## Implementations", ""]
        lines += [f"- `{name}`" for name in self.implementations] or ["- *(none found)*"]
        lines += ["", "## Consumers", ""]
        lines += [f"- `{name}`" for name in self.consumers] or ["- *(none found)*"]
        lines += ["", MARKER_END, ""]
        return "\n".join(lines)


def collect(name: str, module: str, purpose: str) -> Seam:
    """Read one seam out of the source, without importing the whole package."""
    imported = _import(module)
    protocol = getattr(imported, name, None)
    if protocol is None:
        raise SystemExit(f"{module} does not define {name}")

    methods: list[tuple[str, str]] = []
    required: dict[str, frozenset[str]] = {}
    for attribute, value in vars(protocol).items():
        if attribute.startswith("_") or not callable(value):
            continue
        try:
            parameters = inspect.signature(value).parameters
            signature = f"{attribute}{inspect.signature(value)}"
        except (TypeError, ValueError):
            parameters = {}
            signature = attribute
        required[attribute] = frozenset(parameters) - {"self"}
        methods.append((signature, inspect.getdoc(value) or ""))

    return Seam(
        name=name,
        module=f"loom/{module}",
        purpose=purpose,
        doc=inspect.getdoc(protocol) or "",
        methods=sorted(methods),
        implementations=_implementations(name, required),
        consumers=_consumers(name, module),
    )


def _import(module: str) -> Any:
    import importlib

    dotted = "loom." + module.removesuffix(".py").replace("/", ".")
    return importlib.import_module(dotted)


def _implementations(name: str, required: dict[str, frozenset[str]]) -> list[str]:
    """Classes satisfying this protocol, declared or structural.

    Both, because a Protocol is usually satisfied structurally and naming it as
    a base is optional — a page listing only the declared ones would report
    "none found" for exactly the seams whose implementations are cleanest.
    Read from the AST, so generating the catalog imports no optional vendor SDK.

    A structural match needs the method *names* and the **parameter names** of
    each. Names alone are not identifying: ``ExecutionSandbox`` declares one
    method called ``run``, and matching on that reported every check, stage,
    node, and agent backend in the tree as an implementation of it — a page
    that wrong is worse than no page, because it is read as a fact. Parameters
    are what distinguish ``run(body, run_id, input, channel, policy)`` from the
    dozen unrelated ``run(self)`` methods a codebase accumulates.
    """
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name == name:
                continue
            declared = name in {_base_name(base) for base in node.bases}
            if declared or (required and _matches_structurally(node, required)):
                found.append(f"{_module_of(path)}.{node.name}")
    return found


def _matches_structurally(
    node: ast.ClassDef, required: dict[str, frozenset[str]]
) -> bool:
    """Every required method present, each accepting the parameters it declares.

    A superset is allowed — an implementation may take extra optional arguments
    — but every parameter the protocol names must be one the class would accept,
    since a caller holding the protocol will pass them all.
    """
    defined = {
        item.name: item
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for method, parameters in required.items():
        implementation = defined.get(method)
        if implementation is None:
            return False
        args = implementation.args
        accepted = {
            argument.arg
            for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        }
        if args.kwarg is not None:
            # **kwargs accepts any name the protocol declares.
            continue
        if not parameters <= accepted:
            return False
    return True


def _consumers(name: str, module: str) -> list[str]:
    """Modules that import the protocol by name.

    An import is the evidence, read from the AST — a substring search matches
    the word in a docstring and reports half the tree as a consumer.
    """
    own = SRC / module
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == own:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == name for alias in node.names
            ):
                found.append(_module_of(path))
                break
    return found


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _module_of(path: Path) -> str:
    return str(path.relative_to(SRC)).removesuffix(".py").replace("/", ".")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when a page is stale instead of rewriting it",
    )
    args = parser.parse_args()

    DOCS.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, (module, purpose) in SEAMS.items():
        page = DOCS / f"{_slug(name)}.md"
        rendered = collect(name, module, purpose).render()
        current = page.read_text(encoding="utf-8") if page.exists() else ""
        if current == rendered:
            continue
        if args.check:
            stale.append(str(page.relative_to(ROOT)))
        else:
            page.write_text(rendered, encoding="utf-8")
            print(f"wrote {page.relative_to(ROOT)}")

    if stale:
        print(
            "These seam pages no longer match the code:\n  "
            + "\n  ".join(stale)
            + "\n\nRun `python scripts/gen_seam_catalog.py` and commit the result.",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print(f"{len(SEAMS)} seam pages are current")
    return 0


def _slug(name: str) -> str:
    out = [name[0].lower()]
    for char in name[1:]:
        out.append(f"-{char.lower()}" if char.isupper() else char)
    return "".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
