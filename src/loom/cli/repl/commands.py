"""Slash commands, which are the subcommands.

The tempting shape is a registry of small functions calling the facade — and it
is wrong, because it is a *second* implementation of every command. The two
drift, and the drift is invisible: both resolve, both run, and only the one
nobody exercises is wrong.

So a slash command is parsed by **the same argparse parser** and dispatched to
**the same handler** as the command line. ``/run digest -i @payload.json`` is
``loom run digest -i @payload.json``, down to the exit code. Parity is not
maintained here; it is unavailable to break.

What that costs is a fresh ``resolve()`` per command — a new Runtime, and the
project's modules imported again. That is worth paying and is arguably the
correct behaviour rather than a compromise: an authoring session *edits workflow
files*, so re-reading them is what makes ``/run`` run the code you just changed
rather than the code you started with.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

__all__ = ["SLASH", "SlashCommand", "dispatch", "known", "resolve_alias"]


@dataclass(frozen=True)
class SlashCommand:
    """One slash command: what it maps to, and how to describe it."""

    name: str
    #: The subcommand it becomes. Empty for the ones the session handles
    #: itself, which are exactly those that act on the *session* rather than on
    #: a Runtime — there is no ``loom exit``.
    subcommand: str
    summary: str
    #: Arguments appended before the user's own. ``/pending`` is
    #: ``loom pending``; ``/failed`` is ``loom runs --status failed``, which is
    #: the query worth having a name for.
    preset: tuple[str, ...] = ()


#: Session-owned. Deliberately few: everything that touches a Runtime should be
#: a real subcommand, so it is reachable from a script too.
_SESSION_ONLY = (
    SlashCommand("help", "", "Show these commands"),
    SlashCommand("exit", "", "Leave the session"),
    SlashCommand("quit", "", "Leave the session"),
    SlashCommand("clear", "", "Clear the screen"),
    SlashCommand("open", "", "Work on a file: /open flows/digest.py"),
    SlashCommand("new", "", "Stop working on the current file"),
    SlashCommand("status", "", "Project, store, workflows, and what is parked"),
)

_OVER_THE_PORT = (
    SlashCommand("author", "author", "Write a workflow from a description"),
    SlashCommand("edit", "edit", "Change the file in focus by describing the change"),
    SlashCommand("run", "run", "Run a workflow"),
    SlashCommand("runs", "runs", "List runs"),
    SlashCommand("failed", "runs", "Runs that failed", ("--status", "failed")),
    SlashCommand("show", "show", "One run and its journal"),
    SlashCommand("watch", "watch", "Follow a run until it settles"),
    SlashCommand("pending", "pending", "Runs parked on a person"),
    SlashCommand("approve", "approve", "Resolve a pending approval"),
    SlashCommand("respond", "respond", "Answer a parked human request"),
    SlashCommand("send", "send", "Deliver an event to a parked run"),
    SlashCommand("cancel", "cancel", "Cancel a run"),
    SlashCommand("retry", "retry", "Re-run a failure from its failure"),
    SlashCommand("replay", "replay", "Re-execute from the journal"),
    SlashCommand("pin", "pin", "Turn a run into a regression test"),
    SlashCommand("check", "check", "Write the graph and description"),
    SlashCommand("graph", "graph", "Print the workflow graph"),
    SlashCommand("describe", "describe", "Print the narrated description"),
    SlashCommand("workflows", "workflows", "Workflows this project can run"),
    SlashCommand("publish", "publish", "Record a workflow in the catalog"),
    SlashCommand("toolsets", "toolsets", "Integrations reachable here"),
    SlashCommand("toolset", "toolset", "One toolset and its operations"),
    SlashCommand("nodes", "nodes", "Catalogued nodes"),
    SlashCommand("node", "node", "One node, with the code to call it"),
    SlashCommand("artifacts", "artifacts", "What runs produced"),
    SlashCommand("doctor", "doctor", "Check store, model, modules, extras"),
    SlashCommand("connect", "connect", "OAuth a credential a workflow can read"),
    SlashCommand("whoami", "whoami", "What this CLI is connected to"),
)

SLASH: dict[str, SlashCommand] = {
    command.name: command for command in (*_SESSION_ONLY, *_OVER_THE_PORT)
}


def known() -> list[SlashCommand]:
    """Every command, in the order the help should list them."""
    return [*_SESSION_ONLY, *_OVER_THE_PORT]


def resolve_alias(name: str) -> SlashCommand | None:
    """The command *name* refers to, or ``None``.

    Unique prefixes resolve, so ``/work`` is ``/workflows``. An ambiguous one
    does not: ``/c`` matches ``cancel``, ``check`` and ``connect``, and guessing
    which of those somebody meant is how a session cancels a run when asked to
    check one. Prefixes only — an abbreviation like ``/wf`` is a different
    thing, and accepting it would mean deciding what counts as one.

    Two names for the same subcommand are not ambiguous: ``/exit`` and
    ``/quit`` both answer to ``/`` + nothing that distinguishes them, and
    refusing there would be pedantry about a distinction with no consequence.
    """
    if name in SLASH:
        return SLASH[name]
    matches = [command for key, command in SLASH.items() if key.startswith(name)]
    unique = {command.subcommand or command.name for command in matches}
    return matches[0] if len(unique) == 1 and matches else None


def dispatch(line: str, *, defaults: list[str] | None = None) -> int | None:
    """Run a slash command through the real parser. ``None`` if it is not one.

    Returns the handler's exit code, which is the subcommand's exit code —
    ``3`` for a run that parked, and so on. The session prints it rather than
    exiting on it, but the value is the same one a script would branch on.
    """
    from loom.cli import _HANDLERS, build_parser
    from loom.cli.output import Exit

    words = shlex.split(line)
    if not words:
        return None
    command = resolve_alias(words[0].lstrip("/"))
    if command is None or not command.subcommand:
        return None

    argv = [command.subcommand, *command.preset, *words[1:], *(defaults or [])]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse exits the process on a bad argument. In a session that is
        # the one thing it must not do — a typo would end the conversation.
        return Exit.USAGE
    return int(_HANDLERS[args.command](args))
