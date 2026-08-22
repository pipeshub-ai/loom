"""The library must not depend on the CLI.

`loom` and `loomsdk` are two console scripts on **one wheel**, so this is not a
boundary pip enforces — it is a property, and properties creep back one import
at a time. `tests/test_cli_session.py` keeps `repl/commands.py` off the facade
this way, and `test_host_integration.py` keeps a host off `runtime._…`, both
because a module's own tests cannot see it: they construct the thing themselves.

Three tiers, and the middle one is the part that is easy to get wrong:

* **library** — stdlib and declared dependencies. Never `loom.cli`, never a
  `[cli]` extra, never `argparse`.
* **library adapters** — stdlib-only, terminal- or browser-touching, and
  **opted into by a host rather than installed by import**. `CLIUserInteraction`
  reads stdin from `agents/interaction.py`; `OAuthBrowserFlow` opens a browser
  from `connectors/flows.py`. The dividing line is *the extra*, not the
  terminal: `PromptUserInteraction` needs `prompt_toolkit`, so it lives in
  `cli/repl/`.
* **CLI** — everything, `rich` and `prompt_toolkit` and `argparse` included.

This passes on the tree as it stands, so it is a ratchet rather than a cleanup.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "loom"

#: Modules a library file must not import.
#:
#: The three `[cli]`-and-`[tui]` extras, plus `argparse` — which is not an
#: extra but is the shape of a command line, and a library taking a `Namespace`
#: is how every line of the OAuth flow came to be reachable only from `loom
#: connect`.
FORBIDDEN = {"rich", "prompt_toolkit", "textual", "argparse"}


def _library_files() -> list[Path]:
    """Every module outside `loom/cli/`, minus the entry points.

    A `__main__.py` is a program, not a library module: `mcp_server/__main__.py`
    imports `build_parser` because its job is to *be* a command, and
    `toolsets/google/setup.py` is a `python -m` helper whose own docstring says
    "Nothing here is imported by the toolsets."
    """
    return [
        path
        for path in sorted(SRC.rglob("*.py"))
        if "cli" not in path.relative_to(SRC).parts
        and path.name != "__main__.py"
        and path.relative_to(SRC).as_posix() != "toolsets/google/setup.py"
    ]


def _imports(tree: ast.AST) -> set[str]:
    """Module names imported anywhere in *tree*, however they are spelled.

    AST rather than a grep: a lazy `import rich` inside a function is the same
    dependency, and a comment mentioning `argparse` is not one.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def _hard_imports(path: Path) -> set[str]:
    """Imports the module *depends* on — everything not guarded by ImportError.

    The distinction this file exists to make, and the reason it is a rule
    rather than an exemption list. `CLIUserInteraction` lives in the library and
    renders with `rich` **when `rich` happens to be installed**:

        try:
            from rich.console import Console
            ...
        except ImportError:
            _stderr(prompt)          # the stdlib path, which always works

    That is an optional upgrade, not a dependency — the module works on a bare
    `pip install loomsdk`, which is the whole claim. An unguarded import of the
    same name would not.
    """
    tree = ast.parse(path.read_text())
    optional: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(
            handler.type is not None and "ImportError" in ast.unparse(handler.type)
            for handler in node.handlers
        ):
            continue
        for statement in node.body:
            optional |= _imports(statement)
    return _imports(tree) - optional


LIBRARY_FILES = _library_files()
IDS = [p.relative_to(SRC).as_posix() for p in LIBRARY_FILES]


@pytest.mark.parametrize("path", LIBRARY_FILES, ids=IDS)
def test_no_library_module_imports_the_cli(path: Path) -> None:
    offenders = sorted(
        m for m in _hard_imports(path) if m == "loom.cli" or m.startswith("loom.cli.")
    )
    assert not offenders, (
        f"{path.relative_to(SRC)} imports {offenders}. The CLI depends on the "
        "library, not the other way round — move what is shared down, or leave "
        "the rendering in the CLI and pass a callback (see ConnectEvent)."
    )


@pytest.mark.parametrize("path", LIBRARY_FILES, ids=IDS)
def test_no_library_module_imports_a_cli_extra(path: Path) -> None:
    offenders = sorted(m for m in _hard_imports(path) if m.split(".")[0] in FORBIDDEN)
    assert not offenders, (
        f"{path.relative_to(SRC)} imports {offenders}, which is a [cli]/[tui] "
        "extra or argparse, and not behind `except ImportError`. "
        "`pip install loomsdk` does not have them."
    )


def test_the_exemptions_still_name_real_files() -> None:
    """An exemption for a file that has moved is a hole nobody can see."""
    assert (SRC / "mcp_server" / "__main__.py").exists()
    assert (SRC / "toolsets" / "google" / "setup.py").exists()


def test_the_check_would_actually_catch_one(tmp_path: Path) -> None:
    """Guards the guard. A layering test that passes vacuously is worse than
    none, because it reads as coverage."""
    offender = tmp_path / "bad.py"
    offender.write_text("from loom.cli.output import Printer\nimport rich\n")
    found = _hard_imports(offender)
    assert "loom.cli.output" in found
    assert "rich" in found

    # And the guarded shape is *not* reported, or the rule collapses into the
    # exemption list it was written to avoid.
    optional = tmp_path / "ok.py"
    optional.write_text(
        "def render():\n"
        "    try:\n"
        "        import rich\n"
        "    except ImportError:\n"
        "        rich = None\n"
    )
    assert "rich" not in _hard_imports(optional)


def test_importing_the_flows_opens_no_browser_and_reads_no_stdin() -> None:
    """The middle tier's whole contract: available on import, inert until called.

    A subprocess, because the assertion is about what *import* does and
    `sys.modules` is shared with every other test in this run.
    """
    program = """
import getpass, sys, webbrowser

def refuse(*a, **k):
    raise AssertionError("importing loom.connectors.flows reached the outside world")

webbrowser.open = refuse
webbrowser.open_new = refuse
webbrowser.open_new_tab = refuse
getpass.getpass = refuse
sys.stdin = None

import loom.connectors.flows as flows

# Constructing the adapters must be inert too — only `connect()` acts.
flows.OAuthBrowserFlow()
flows.OAuthDeviceFlow()
flows.ApiKeyFlow()
flows.ConsoleSecretPrompt()

# Not "rich is absent from sys.modules": a third-party package in the
# environment may pull it in for its own reasons, and asserting that would
# test the venv rather than this code. What the *import* must not do is drag
# in the CLI, or touch a browser or a terminal — which the stubs above prove.
assert not [m for m in sys.modules if m.startswith("loom.cli")], "flows imported the CLI"
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
