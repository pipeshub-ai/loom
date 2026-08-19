"""``loom setup`` — write this install into Claude/Cursor/Codex's MCP config.

Each of these clients spawns an MCP server as a child process over stdio and
reads *where* to find it from its own config file. Wiring LOOM in by hand
means finding the right file, in the right format, in the right place for
the OS, without breaking any other server already listed there. This module
does that once, for all three clients, and is deliberately dumb about
authentication: the generated entry always spawns ``loom mcp`` over stdio,
even when ``--server`` points at a remote Runtime, because the *outer*
client-to-child channel is stdio regardless of what the child does next —
bearer tokens for a remote server come from ``loom login``, already on this
machine, not from anything written into a client's config file.

Codex's config is TOML; Claude Desktop's and Cursor's are JSON with a
different top-level key. Both are read back (never blindly overwritten) so a
second ``loom setup`` — or a client's own settings UI — does not clobber
other servers already configured.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.cli.commands import printer_for
from loom.cli.output import Exit

__all__ = ["CLIENTS", "ClientSpec", "build_entry", "cmd_setup", "merge_config"]


@dataclass(frozen=True)
class ClientSpec:
    """How one MCP client stores its server list."""

    id: str
    display_name: str
    format: str  # "json" or "toml"
    servers_key: str  # "mcpServers" (Claude/Cursor) or "mcp_servers" (Codex)
    global_path: Path
    project_relative: str | None  # None when the client has no project scope


def _claude_desktop_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


CLIENTS: dict[str, ClientSpec] = {
    "claude": ClientSpec(
        id="claude",
        display_name="Claude Desktop",
        format="json",
        servers_key="mcpServers",
        global_path=_claude_desktop_path(),
        project_relative=None,
    ),
    "cursor": ClientSpec(
        id="cursor",
        display_name="Cursor",
        format="json",
        servers_key="mcpServers",
        global_path=Path.home() / ".cursor" / "mcp.json",
        project_relative=".cursor/mcp.json",
    ),
    "codex": ClientSpec(
        id="codex",
        display_name="Codex",
        format="toml",
        servers_key="mcp_servers",
        global_path=Path.home() / ".codex" / "config.toml",
        project_relative=None,
    ),
}


def config_path(spec: ClientSpec, *, use_global: bool, project_dir: Path) -> Path:
    """Where this client's config lives, honouring ``--global``/``--path``.

    Falls back to the global path for a client with no project scope (Codex,
    Claude Desktop) even when ``--global`` was not passed, since there is
    nowhere else for it to go.
    """
    if not use_global and spec.project_relative is not None:
        return project_dir / spec.project_relative
    return spec.global_path


def _loom_command() -> str:
    """Absolute path to this install's ``loom``.

    GUI clients (Cursor, Claude Desktop) do not inherit a developer shell's
    PATH, so a bare ``loom`` in the config is how the server silently never
    starts. Prefer the running script, then ``PATH``, then the bare name as
    a last resort for tests and unusual installs.
    """
    argv0 = Path(sys.argv[0]).expanduser()
    try:
        resolved = argv0.resolve()
    except OSError:
        resolved = argv0
    if resolved.name in {"loom", "loomsdk"} and resolved.is_file():
        return str(resolved)
    return shutil.which("loom") or "loom"


def build_entry(
    *,
    module: list[str] | None,
    server: str | None,
    name: str,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    """The ``{"command": ..., "args": [...]}`` object every client expects.

    Always ``loom mcp`` over stdio — see the module docstring for why
    ``--server`` does not change the command, only its arguments.
    """
    args: list[str] = ["mcp"]
    for mod in module or []:
        args.extend(["--module", mod])
    if server:
        args.extend(["--server", server])
    if name != "loom":
        args.extend(["--name", name])
    entry: dict[str, Any] = {"command": _loom_command(), "args": args}
    if cwd is not None:
        entry["cwd"] = str(Path(cwd).resolve())
    return entry


def _load_existing(path: Path, spec: ClientSpec) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    if spec.format == "toml":
        return tomllib.loads(text)
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def merge_config(
    existing: dict[str, Any], spec: ClientSpec, name: str, entry: dict[str, Any]
) -> dict[str, Any]:
    """*existing* with ``servers_key.name`` replaced by *entry*, everything
    else — other servers, unrelated top-level keys — left exactly as read."""
    merged = dict(existing)
    servers = dict(merged.get(spec.servers_key) or {})
    servers[name] = entry
    merged[spec.servers_key] = servers
    return merged


def _dump_toml(data: dict[str, Any], *, _indent: str = "") -> str:
    """Serialize the narrow shape this command produces: nested tables of
    tables holding only strings, bools, and lists of strings.

    Good enough to round-trip through :mod:`tomllib` for what LOOM itself
    writes; not a general TOML writer. Pulling in a third-party TOML library
    for one nested dict was not worth a new hard dependency.

    A purely-container table (one holding only other tables, like the
    top-level ``mcp_servers``) never gets its own ``[header]`` line — TOML
    does not require one, and skipping it means the file goes straight to
    ``[mcp_servers.loom]`` instead of an empty ``[mcp_servers]`` above it.
    """
    scalar_lines = [
        f"{key} = {_toml_scalar(value)}"
        for key, value in data.items()
        if not isinstance(value, dict)
    ]
    table_blocks = [(k, v) for k, v in data.items() if isinstance(v, dict)]

    lines = list(scalar_lines)
    for key, value in table_blocks:
        header = f"{_indent}.{key}" if _indent else key
        if scalar_lines or _indent:
            # A leaf-with-siblings, or any non-root table, needs its header
            # written even when this particular child is itself a container.
            lines.append("")
            lines.append(f"[{header}]")
            lines.append(_dump_toml(value, _indent=header).strip())
        else:
            lines.append(_dump_toml(value, _indent=header).strip())
    return "\n".join(lines).strip() + "\n"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    return json.dumps(value)


def _dump(data: dict[str, Any], spec: ClientSpec) -> str:
    if spec.format == "toml":
        return _dump_toml(data)
    return json.dumps(data, indent=2) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def cmd_setup(args: argparse.Namespace) -> int:
    """Write (or update) an MCP client's config to launch this install."""
    out = printer_for(args)
    requested = list(CLIENTS) if args.client == "all" else [args.client]

    if args.path and args.client == "all":
        out.error("--path names one file; pick a single client, not 'all'")
        return Exit.USAGE

    project_dir = Path(args.project) if args.project else Path.cwd()
    entry = build_entry(
        module=args.module, server=args.server, name=args.name, cwd=project_dir
    )

    results: list[dict[str, Any]] = []
    for client_id in requested:
        spec = CLIENTS[client_id]
        path = (
            Path(args.path)
            if args.path
            else config_path(spec, use_global=args.global_, project_dir=project_dir)
        )
        try:
            existing = _load_existing(path, spec)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            out.error(f"{spec.display_name}: could not parse {path} ({exc}) — leaving it untouched")
            continue

        merged = merge_config(existing, spec, args.name, entry)
        text = _dump(merged, spec)

        if not args.dry_run:
            _write(path, text)

        results.append(
            {
                "client": client_id,
                "path": str(path),
                "written": not args.dry_run,
                "entry": entry,
            }
        )
        if args.dry_run:
            out.hint(f"{spec.display_name} -> {path}")
            if not args.json:
                # Printed raw, bypassing Printer.line's rich markup: generated
                # TOML/JSON routinely contains a bare "[...]" (a table header,
                # an array literal) that rich would otherwise try to parse as
                # a style tag and silently swallow.
                print(text.rstrip())
        else:
            out.line(f"  wrote {spec.display_name} config: [dim]{path}[/dim]")

    out.json({"configured": results})
    return Exit.OK
