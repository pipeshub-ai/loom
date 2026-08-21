"""The CLI guide cannot document a command or flag the parser does not have.

``scripts/docs_examples.py`` executes the ``python`` blocks on every guide,
which is the check that catches a wrong API. A CLI page is ``bash``, so none of
it was covered — and a page of shell commands has exactly the same failure
mode: a flag gets renamed, the page keeps promising the old one, and the first
person to find out is someone following the docs.

Executing the blocks is the wrong answer here. Half of them start runs, spend
model tokens, or open OAuth in a browser. What can be checked without running
anything is the part that actually rots: every ``loom <subcommand>`` the page
names exists, and every ``--flag`` it shows that subcommand taking is one the
subcommand accepts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from loom.cli import _HANDLERS, build_parser

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "guides" / "cli.md"

#: ```bash blocks, and inline `loom …` spans — the page uses both.
_FENCE = re.compile(r"```bash\n(.*?)```", re.S)
_INLINE = re.compile(r"`(loom [^`\n]+)`")

#: A line that is a `loom` invocation, with the comment and any shell
#: decoration stripped.
_INVOCATION = re.compile(r"^\s*(?:\$\s*)?(loom\s+[^\n#|>]+)")

#: Placeholders the page uses for values a reader substitutes. Not flags, and
#: not subcommands.
_PLACEHOLDER = re.compile(r"^[<\"'$@]")


def invocations() -> list[str]:
    """Every ``loom …`` command the guide shows."""
    text = GUIDE.read_text(encoding="utf-8")
    found: list[str] = []
    for block in _FENCE.findall(text):
        for line in block.splitlines():
            match = _INVOCATION.match(line)
            if match:
                found.append(match.group(1).strip())
    found += [span.strip() for span in _INLINE.findall(text)]
    return found


def parts(command: str) -> tuple[str, list[str]]:
    """A command's subcommand and the long flags it passes."""
    words = command.split()[1:]  # drop "loom"
    subcommand = ""
    for word in words:
        if not word.startswith("-") and not _PLACEHOLDER.match(word):
            subcommand = word
            break
    flags = [w.split("=")[0] for w in words if w.startswith("--")]
    return subcommand, flags


def test_the_guide_exists() -> None:
    """It did not, for the whole life of the CLI.

    Twelve guides in ``docs/guides`` and none of them about the surface most
    people meet first; ``getting-started.md`` is Python-first and never
    mentions ``loom``.
    """
    assert GUIDE.exists()


def test_it_shows_something() -> None:
    """Guards every other test here against a page that stopped having
    examples, which would make all of them pass."""
    assert len(invocations()) >= 40


@pytest.mark.parametrize("command", invocations())
def test_every_documented_command_exists(command: str) -> None:
    subcommand, _ = parts(command)
    if not subcommand:
        # `loom` on its own opens a session, which the page shows deliberately.
        return
    assert subcommand in _HANDLERS, (
        f"the CLI guide shows `{command}`, and there is no `{subcommand}` "
        "subcommand"
    )


@pytest.mark.parametrize("command", invocations())
def test_every_documented_flag_is_accepted(command: str) -> None:
    """The failure this exists for: a renamed flag the page still promises."""
    subcommand, flags = parts(command)
    if not subcommand or not flags:
        return
    parser = build_parser()
    known = _flags_of(parser, subcommand)
    for flag in flags:
        assert flag in known, (
            f"the CLI guide shows `{command}`, and `loom {subcommand}` does "
            f"not accept `{flag}`. Known: {', '.join(sorted(known))}"
        )


def _flags_of(parser, subcommand: str) -> set[str]:
    for action in parser._subparsers._group_actions:  # type: ignore[union-attr]
        sub = (action.choices or {}).get(subcommand)
        if sub is None:
            continue
        return {
            option
            for entry in sub._actions
            for option in entry.option_strings
            if option.startswith("--")
        }
    return set()


def test_the_exit_code_table_matches_the_enum() -> None:
    """A table of exit codes is the one part of a CLI page a script depends on."""
    from loom.cli.output import Exit

    text = GUIDE.read_text(encoding="utf-8")
    for code, meaning in (
        (Exit.OK, "completed"),
        (Exit.FAILED, "failed"),
        (Exit.USAGE, "usage"),
        (Exit.SUSPENDED, "suspended"),
        (Exit.CANCELLED, "cancelled"),
    ):
        row = re.search(rf"\|\s*`{int(code)}`[^|]*\|([^|]*)\|", text)
        assert row is not None, f"no row for exit code {int(code)}"
        assert meaning in row.group(1).lower(), (
            f"exit {int(code)} is {meaning!r} in the enum; the guide says "
            f"{row.group(1).strip()!r}"
        )


def test_every_command_appears_in_the_help() -> None:
    """Forty commands in a flat argparse list, with `author` and `run` sixth
    and seventh, is a list nobody reads to the end. Grouping them is only worth
    anything if nothing falls out of the groups on the way."""
    from loom.cli import _EPILOG, _HANDLERS

    missing = [name for name in _HANDLERS if name not in _EPILOG]
    assert not missing, f"these commands appear in no help group: {missing}"


def test_the_help_fits_a_screen() -> None:
    """The reason for grouping, stated as a number."""
    from loom.cli import build_parser

    assert len(build_parser().format_help().splitlines()) <= 40


def test_every_setup_client_it_names_exists() -> None:
    from loom.cli import mcp_setup

    text = GUIDE.read_text(encoding="utf-8")
    for name in re.findall(r"loom setup (\S+)", text):
        assert name == "all" or mcp_setup.client_named(name) is not None, (
            f"the guide shows `loom setup {name}`, which is not a client"
        )


class TestCompletionIsGeneratedFromTheParser:
    """Forty commands restated in three shell dialects is three copies to
    forget, and the failure is silent: completion offers a flag the CLI
    rejects, which reads as the shell being broken."""

    def test_every_command_is_offered(self) -> None:
        from loom.cli import _HANDLERS
        from loom.cli.completion import commands

        assert set(commands()) == set(_HANDLERS)

    def test_flags_come_from_the_subparser(self) -> None:
        from loom.cli.completion import flags_of

        assert "--follow" in flags_of("run")
        assert "--detach" in flags_of("run")
        # And not from some other command's.
        assert "--reject" not in flags_of("run")

    def test_an_unknown_subcommand_has_no_flags(self) -> None:
        from loom.cli.completion import flags_of

        assert flags_of("no-such-command") == []

    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_each_script_mentions_every_command(self, shell: str) -> None:
        from loom.cli import _HANDLERS
        from loom.cli.completion import script_for

        script = script_for(shell)
        missing = [name for name in _HANDLERS if name not in script]
        assert not missing, f"{shell} completion omits {missing}"

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_the_script_parses_in_its_own_shell(self, shell: str) -> None:
        """A completion script that does not parse is worse than none: it
        breaks the shell's completion for everything, not only for loom."""
        import shutil
        import subprocess
        import tempfile

        binary = shutil.which(shell)
        if binary is None:  # pragma: no cover - depends on the machine
            pytest.skip(f"{shell} is not installed")

        from loom.cli.completion import script_for

        with tempfile.NamedTemporaryFile("w", suffix=f".{shell}") as handle:
            handle.write(script_for(shell))
            handle.flush()
            done = subprocess.run(
                [binary, "-n", handle.name], capture_output=True, text=True
            )
        assert done.returncode == 0, done.stderr

    def test_an_unknown_shell_is_refused(self) -> None:
        from loom.cli.completion import script_for

        with pytest.raises(ValueError, match="unknown shell"):
            script_for("csh")
