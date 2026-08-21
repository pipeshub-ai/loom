"""The interactive session, and the slash commands that reach the subcommands.

Split three ways so the parts that need ``prompt_toolkit`` are the only ones
that import it: :mod:`commands` is a mapping onto the real argparse parser and
needs nothing, :mod:`complete` and :mod:`session` are the terminal.
"""

from __future__ import annotations

__all__ = ["available", "run_session"]


def available() -> bool:
    """Whether a session can be opened in this install.

    Checked before ``loom`` with no subcommand decides between opening one and
    printing help, so a missing extra produces the install line rather than an
    ImportError from inside the loop.
    """
    import importlib.util

    return importlib.util.find_spec("prompt_toolkit") is not None


def run_session(args: object) -> int:
    from loom.cli.repl.session import run_session as _run

    return _run(args)
