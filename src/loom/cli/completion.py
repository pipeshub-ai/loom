"""Shell completion, emitted rather than installed.

``loom completion zsh >> ~/.zshrc`` — printed to stdout so the user decides
where it goes. A CLI that edits shell startup files is one people uninstall
carefully.

**Generated from the parser, never from a list here.** Forty commands and
their flags restated in three shell dialects is three copies to forget when a
flag is renamed, and the failure is silent: completion offers a flag the CLI
rejects, which reads as the shell being broken. :func:`commands` and
:func:`flags_of` read ``build_parser()``, so the script is a projection of the
parser the way the graph is a projection of the code.

Dynamic values — workflow names, run ids — are completed by calling back into
``loom`` rather than being baked in, because both change while the shell is
open. Each callback is a real command with ``--json``, so it degrades to no
completions rather than to garbage when the project cannot be read.
"""

from __future__ import annotations

import argparse
from typing import Any

from loom.cli.output import Exit, Printer

__all__ = ["SHELLS", "cmd_completion", "commands", "flags_of", "script_for"]

SHELLS = ("bash", "zsh", "fish")

#: Subcommands whose first positional argument is a run id. Completing those
#: needs the store, which is why they are named rather than guessed: `approve`
#: takes a run *and* a subject, and offering run ids for the subject would be
#: worse than offering nothing.
_TAKES_RUN = (
    "show",
    "watch",
    "cancel",
    "retry",
    "replay",
    "pause",
    "unpause",
    "pin",
    "approve",
    "respond",
    "send",
)

#: Subcommands whose first positional argument is a workflow name.
_TAKES_WORKFLOW = ("run", "publish", "versions")

#: How each dynamic list is fetched. A real command with ``--json``, so a
#: project that cannot be read yields no completions rather than garbage —
#: and `sed` rather than `jq`, which is not on every machine.
_WORKFLOWS = (
    'loom workflows --json 2>/dev/null'
    ' | sed -n \'s/.*"name": "\\([^"]*\\)".*/\\1/p\''
)
_RUNS = (
    'loom runs --json -n 50 2>/dev/null'
    ' | sed -n \'s/.*"run_id": "\\([^"]*\\)".*/\\1/p\''
)


def commands() -> list[str]:
    """Every subcommand, read from the parser."""
    from loom.cli import build_parser

    return sorted(_subparsers(build_parser()))


def flags_of(subcommand: str) -> list[str]:
    """Every option string one subcommand accepts, read from the parser."""
    from loom.cli import build_parser

    sub = _subparsers(build_parser()).get(subcommand)
    if sub is None:
        return []
    return sorted(
        option for action in sub._actions for option in action.option_strings
    )


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, Any]:
    for action in parser._subparsers._group_actions:  # type: ignore[union-attr]
        if action.choices:
            return dict(action.choices)
    return {}


def script_for(shell: str) -> str:
    """The completion script for *shell*."""
    if shell == "bash":
        return _bash()
    if shell == "zsh":
        return _zsh()
    if shell == "fish":
        return _fish()
    raise ValueError(f"unknown shell {shell!r}; expected one of {', '.join(SHELLS)}")


def cmd_completion(args: argparse.Namespace) -> int:
    """Print the script. Writing it anywhere is the user's decision."""
    out = Printer(as_json=getattr(args, "json", False))
    try:
        script = script_for(args.shell)
    except ValueError as exc:
        out.error(str(exc))
        return Exit.USAGE
    out.json({"shell": args.shell, "script": script})
    # `verbatim`: this is a shell script, and it is dense with the brackets and
    # braces a markup parser reads as instructions.
    out.verbatim(script)
    return Exit.OK


# ---------------------------------------------------------------------------
# The dialects
# ---------------------------------------------------------------------------
#
# Each is a small template over the same four facts: the command list, the
# per-command flags, which commands take a run id, and which take a workflow
# name. Only the syntax differs.


def _cases(render: Any) -> str:
    return "\n".join(render(name) for name in commands())


def _bash() -> str:
    flag_cases = _cases(
        lambda name: f'    {name}) opts="{" ".join(flags_of(name))}" ;;'
    )
    return f"""# loom completion for bash — eval "$(loom completion bash)"
_loom_completion() {{
  local cur prev words cword opts
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  local sub=""
  local i
  for (( i=1; i < COMP_CWORD; i++ )); do
    case "${{COMP_WORDS[i]}}" in
      -*) ;;
      *) sub="${{COMP_WORDS[i]}}"; break ;;
    esac
  done

  if [[ -z "$sub" ]]; then
    COMPREPLY=( $(compgen -W "{" ".join(commands())}" -- "$cur") )
    return
  fi

  if [[ "$cur" == -* ]]; then
    case "$sub" in
{flag_cases}
    esac
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    return
  fi

  case "$sub" in
    {"|".join(_TAKES_WORKFLOW)})
      COMPREPLY=( $(compgen -W "$(_loom_workflows)" -- "$cur") ) ;;
    {"|".join(_TAKES_RUN)})
      COMPREPLY=( $(compgen -W "$(_loom_runs)" -- "$cur") ) ;;
    *) COMPREPLY=( $(compgen -f -- "$cur") ) ;;
  esac
}}

# Dynamic, because both change while the shell is open. Failure is silence.
_loom_workflows() {{ {_WORKFLOWS}; }}
_loom_runs() {{ {_RUNS}; }}

complete -F _loom_completion loom loomsdk
"""


def _zsh() -> str:
    flag_cases = _cases(
        lambda name: f'      {name}) opts=({" ".join(flags_of(name))}) ;;'
    )
    return f"""# loom completion for zsh — eval "$(loom completion zsh)"
_loom_completion() {{
  local -a opts
  local sub="" word
  for word in "${{words[@]:1:$((CURRENT-2))}}"; do
    [[ "$word" == -* ]] || {{ sub="$word"; break }}
  done

  if [[ -z "$sub" ]]; then
    compadd -- {" ".join(commands())}
    return
  fi

  if [[ "${{words[CURRENT]}}" == -* ]]; then
    case "$sub" in
{flag_cases}
    esac
    compadd -- "${{opts[@]}}"
    return
  fi

  case "$sub" in
    {"|".join(_TAKES_WORKFLOW)})
      compadd -- ${{(f)"$({_WORKFLOWS})"}} ;;
    {"|".join(_TAKES_RUN)})
      compadd -- ${{(f)"$({_RUNS})"}} ;;
    *) _files ;;
  esac
}}

compdef _loom_completion loom loomsdk
"""


def _fish() -> str:
    lines = [
        "# loom completion for fish — loom completion fish > "
        "~/.config/fish/completions/loom.fish",
        "",
        "function __loom_no_subcommand",
        "    for word in (commandline -opc)[2..-1]",
        "        string match -qv -- '-*' $word; and return 1",
        "    end",
        "    return 0",
        "end",
        "",
        "function __loom_workflows",
        f"    {_WORKFLOWS}",
        "end",
        "",
        "function __loom_runs",
        f"    {_RUNS}",
        "end",
        "",
    ]
    from loom.cli import build_parser

    subparsers = _subparsers(build_parser())
    for name in commands():
        summary = _summary(subparsers.get(name))
        lines.append(
            f"complete -c loom -n __loom_no_subcommand -a {name} -d {_quote(summary)}"
        )
    lines.append("")
    for name in commands():
        for flag in flags_of(name):
            if flag.startswith("--"):
                lines.append(
                    f"complete -c loom -n '__fish_seen_subcommand_from {name}' "
                    f"-l {flag[2:]}"
                )
    lines.append("")
    for name in _TAKES_WORKFLOW:
        lines.append(
            f"complete -c loom -n '__fish_seen_subcommand_from {name}' "
            "-a '(__loom_workflows)'"
        )
    for name in _TAKES_RUN:
        lines.append(
            f"complete -c loom -n '__fish_seen_subcommand_from {name}' "
            "-a '(__loom_runs)'"
        )
    return "\n".join(lines) + "\n"


def _summary(parser: Any) -> str:
    """One line describing a subcommand, from its own parser."""
    return str(getattr(parser, "description", "") or "").split("\n")[0][:60]


def _quote(text: str) -> str:
    return "'" + text.replace("'", r"\'") + "'"
