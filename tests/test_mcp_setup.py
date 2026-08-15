"""``loom setup`` — writing Claude/Cursor/Codex's MCP config.

Two things matter for each of the three clients: the generated file parses
back and matches that client's documented schema (``mcpServers`` JSON for
Claude/Cursor, ``mcp_servers`` TOML for Codex), and a second run merges
rather than clobbers whatever else was already in the file.
"""

from __future__ import annotations

import dataclasses
import json
import tomllib
from pathlib import Path

import pytest

from loom.cli import build_parser
from loom.cli.mcp_setup import (
    CLIENTS,
    build_entry,
    cmd_setup,
    config_path,
    merge_config,
)


def _args(tmp_path: Path, client: str, **overrides):
    parser = build_parser()
    argv = ["setup", client]
    ns = parser.parse_args(argv)
    ns.module = overrides.get("module")
    ns.server = overrides.get("server")
    ns.name = overrides.get("name", "loom")
    ns.global_ = overrides.get("global_", False)
    ns.project = overrides.get("project")
    ns.path = overrides.get("path", str(tmp_path / f"{client}_config"))
    ns.dry_run = overrides.get("dry_run", False)
    ns.json = overrides.get("json", False)
    return ns


def _is_loom_command(command: str) -> bool:
    return Path(command).name in {"loom", "loomflow"}


class TestBuildEntry:
    def test_bare_entry_spawns_loom_mcp(self) -> None:
        entry = build_entry(module=None, server=None, name="loom")
        assert _is_loom_command(entry["command"])
        assert entry["args"] == ["mcp"]
        assert "cwd" not in entry

    def test_module_becomes_a_repeatable_flag(self) -> None:
        entry = build_entry(module=["a.py", "b.py"], server=None, name="loom")
        assert entry["args"] == ["mcp", "--module", "a.py", "--module", "b.py"]

    def test_server_is_still_spawned_over_stdio(self) -> None:
        """A remote Runtime changes what the child does, not how the client
        talks to it — the client always spawns a process over stdio."""
        entry = build_entry(module=None, server="https://loom.example", name="loom")
        assert _is_loom_command(entry["command"])
        assert "--server" in entry["args"]

    def test_a_non_default_name_is_passed_through(self) -> None:
        entry = build_entry(module=None, server=None, name="loom-prod")
        assert entry["args"][-2:] == ["--name", "loom-prod"]

    def test_cwd_is_recorded_so_relative_modules_resolve(self, tmp_path: Path) -> None:
        entry = build_entry(module=["flows.py"], server=None, name="loom", cwd=tmp_path)
        assert entry["cwd"] == str(tmp_path.resolve())


class TestConfigPath:
    def test_cursor_defaults_to_the_project_directory(self, tmp_path: Path) -> None:
        path = config_path(CLIENTS["cursor"], use_global=False, project_dir=tmp_path)
        assert path == tmp_path / ".cursor" / "mcp.json"

    def test_cursor_global_flag_uses_the_home_path(self, tmp_path: Path) -> None:
        path = config_path(CLIENTS["cursor"], use_global=True, project_dir=tmp_path)
        assert path == Path.home() / ".cursor" / "mcp.json"

    def test_codex_has_no_project_scope(self, tmp_path: Path) -> None:
        """Passing use_global=False still resolves to the global path — Codex
        has nowhere else to put it."""
        path = config_path(CLIENTS["codex"], use_global=False, project_dir=tmp_path)
        assert path == CLIENTS["codex"].global_path

    def test_claude_desktop_has_no_project_scope(self, tmp_path: Path) -> None:
        path = config_path(CLIENTS["claude"], use_global=False, project_dir=tmp_path)
        assert path == CLIENTS["claude"].global_path


class TestMergeConfig:
    def test_an_empty_existing_file_gets_one_server(self) -> None:
        entry = {"command": "loom", "args": ["mcp"]}
        merged = merge_config({}, CLIENTS["cursor"], "loom", entry)
        assert merged == {"mcpServers": {"loom": entry}}

    def test_other_servers_and_top_level_keys_survive_a_merge(self) -> None:
        entry = {"command": "loom", "args": ["mcp"]}
        existing = {
            "mcpServers": {"other": {"command": "foo", "args": []}},
            "unrelatedTopLevelKey": True,
        }
        merged = merge_config(existing, CLIENTS["cursor"], "loom", entry)

        assert merged["mcpServers"]["other"] == {"command": "foo", "args": []}
        assert merged["mcpServers"]["loom"] == entry
        assert merged["unrelatedTopLevelKey"] is True

    def test_re_running_replaces_only_the_loom_entry(self) -> None:
        old_entry = {"command": "loom", "args": ["mcp", "--module", "old.py"]}
        new_entry = {"command": "loom", "args": ["mcp", "--module", "new.py"]}
        first = merge_config({}, CLIENTS["cursor"], "loom", old_entry)
        second = merge_config(first, CLIENTS["cursor"], "loom", new_entry)
        assert second["mcpServers"]["loom"] == new_entry


class TestClientConfigRoundTrip:
    """The property the plan calls out explicitly: a generated config is
    parsed back and matches each client's documented shape."""

    def test_claude_desktop_json_round_trips_as_mcp_servers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "claude_desktop_config.json"
        cmd_setup(_args(tmp_path, "claude", module=["flows.py"], path=str(path)))

        parsed = json.loads(path.read_text())
        assert "mcpServers" in parsed
        assert _is_loom_command(parsed["mcpServers"]["loom"]["command"])
        assert parsed["mcpServers"]["loom"]["args"] == ["mcp", "--module", "flows.py"]
        assert "cwd" in parsed["mcpServers"]["loom"]

    def test_cursor_json_round_trips_as_mcp_servers(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        cmd_setup(_args(tmp_path, "cursor", module=["flows.py"], path=str(path)))

        parsed = json.loads(path.read_text())
        assert {"command", "args", "cwd"} <= set(parsed["mcpServers"]["loom"])

    def test_codex_toml_round_trips_as_mcp_servers(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        cmd_setup(
            _args(tmp_path, "codex", module=["flows.py", "extra.py"], path=str(path))
        )

        parsed = tomllib.loads(path.read_text())
        assert "mcp_servers" in parsed
        assert _is_loom_command(parsed["mcp_servers"]["loom"]["command"])
        assert parsed["mcp_servers"]["loom"]["args"] == [
            "mcp",
            "--module",
            "flows.py",
            "--module",
            "extra.py",
        ]

    def test_codex_toml_with_a_remote_server_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        cmd_setup(
            _args(tmp_path, "codex", server="https://loom.example", path=str(path))
        )
        parsed = tomllib.loads(path.read_text())
        assert "--server" in parsed["mcp_servers"]["loom"]["args"]

    def test_merging_into_a_pre_existing_codex_file_preserves_other_servers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[mcp_servers.other]\ncommand = "foo"\nargs = []\n', encoding="utf-8"
        )
        cmd_setup(_args(tmp_path, "codex", module=["flows.py"], path=str(path)))

        parsed = tomllib.loads(path.read_text())
        assert parsed["mcp_servers"]["other"] == {"command": "foo", "args": []}
        assert _is_loom_command(parsed["mcp_servers"]["loom"]["command"])

    def test_merging_into_a_pre_existing_cursor_file_preserves_other_servers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps({"mcpServers": {"other": {"command": "foo", "args": []}}}),
            encoding="utf-8",
        )
        cmd_setup(_args(tmp_path, "cursor", module=["flows.py"], path=str(path)))

        parsed = json.loads(path.read_text())
        assert parsed["mcpServers"]["other"] == {"command": "foo", "args": []}
        assert _is_loom_command(parsed["mcpServers"]["loom"]["command"])


class TestDryRun:
    def test_dry_run_never_writes_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        cmd_setup(
            _args(tmp_path, "cursor", module=["flows.py"], path=str(path), dry_run=True)
        )
        assert not path.exists()

    def test_json_mode_reports_written_false_for_a_dry_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "mcp.json"
        cmd_setup(
            _args(
                tmp_path,
                "cursor",
                module=["flows.py"],
                path=str(path),
                dry_run=True,
                json=True,
            )
        )
        out = json.loads(capsys.readouterr().out)
        assert out["configured"][0]["written"] is False


class TestAllClients:
    def test_all_rejects_an_explicit_path(self, tmp_path: Path) -> None:
        from loom.cli.output import Exit

        args = _args(tmp_path, "all", path=str(tmp_path / "x"))
        assert cmd_setup(args) == Exit.USAGE

    def test_all_configures_every_known_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redirect the global-only clients' paths into tmp_path first — 'all'
        must never touch a developer's real Claude Desktop/Codex config."""
        import loom.cli.mcp_setup as mcp_setup

        patched = {
            client_id: dataclasses.replace(
                spec,
                global_path=tmp_path / f"{client_id}_global",
                # A leading dot would make this a hidden directory; steering
                # clear of that here is only to dodge this sandbox's separate
                # restriction on creating dot-directories, unrelated to what
                # this test is actually verifying (that "all" reaches every
                # client and writes each one).
                project_relative=(
                    "cursor_dir/mcp.json" if client_id == "cursor" else None
                ),
            )
            for client_id, spec in CLIENTS.items()
        }
        monkeypatch.setattr(mcp_setup, "CLIENTS", patched)

        args = _args(tmp_path, "all", module=["flows.py"], project=str(tmp_path))
        args.path = None
        args.global_ = False
        cmd_setup(args)

        assert (tmp_path / "cursor_dir" / "mcp.json").exists()
        assert (tmp_path / "claude_global").exists()
        assert (tmp_path / "codex_global").exists()


class TestCliWiring:
    def test_setup_is_a_registered_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["setup", "cursor", "--module", "flows.py"])
        assert args.command == "setup"
        assert args.client == "cursor"
        assert args.module == ["flows.py"]

    def test_client_is_restricted_to_known_values(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["setup", "not-a-real-client"])
