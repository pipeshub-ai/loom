"""Where a project keeps its runs, and how it says so.

The default store was ``memory://``, so every ``loom`` process built its own
journal and a run recorded by one invocation did not exist for the next. Twelve
commands depend on state outliving a process and all twelve answered "none" out
of the box; ``--detach`` printed an identifier for something already gone.

The fix is a resolution order rather than a new default, so these tests are
mostly about precedence: an explicit choice must still win, at every level, or
the fix has taken a decision away from somebody who had made it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from loom.cli.config import ProjectConfig, load_dotenv
from loom.cli.output import Exit

FLOWS = '''
from __future__ import annotations

from loom import Context, step, workflow


@step
async def echo(text: str) -> str:
    return text


@workflow(name="keeper")
async def keeper(ctx: Context, text: str = "kept") -> str:
    """Something to leave behind in the store."""
    return await ctx.step(echo, text)
'''

PYPROJECT = """
[project]
name = "projecttest"
version = "0.1.0"

[tool.loom]
modules = ["flows.py"]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "flows.py").write_text(FLOWS)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient ``LOOM_STORE``, so precedence is actually being tested."""
    monkeypatch.delenv("LOOM_STORE", raising=False)


def loom(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI with **no** store in the environment.

    Every other CLI suite passes ``LOOM_STORE`` explicitly, which is exactly
    what these tests must not do — the point is what happens when nobody says.
    """
    env = {k: v for k, v in os.environ.items() if k != "LOOM_STORE"}
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )


class TestStoreResolution:
    """Most explicit wins, at every level."""

    @pytest.mark.usefixtures("clean_env")
    def test_a_project_gets_a_store_beside_its_pyproject(
        self, project: Path
    ) -> None:
        config = ProjectConfig.discover(project)
        assert config.root == project
        assert config.store_url == f"sqlite://{project / '.loom' / 'runs.db'}"
        assert not config.ephemeral

    @pytest.mark.usefixtures("clean_env")
    def test_no_project_stays_ephemeral(
        self, tmp_path: Path
    ) -> None:
        """Narrow on purpose: nowhere to write is not the same as not wanting to.

        A scratch shell or a pipe has no directory it has been invited to put a
        file in, and manufacturing one would be worse than forgetting.
        """
        bare = tmp_path / "nothing-here"
        bare.mkdir()
        config = ProjectConfig.discover(bare)
        assert config.root is None
        assert config.ephemeral
        assert "nothing persists" in config.store_source

    def test_the_environment_beats_the_project(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_STORE", "sqlite://from-env.db")
        config = ProjectConfig.discover(project)
        assert config.store_url == "sqlite://from-env.db"
        assert config.store_source.startswith("$LOOM_STORE")

    def test_the_flag_beats_the_environment(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_STORE", "sqlite://from-env.db")
        config = ProjectConfig.discover(project, store="sqlite://from-flag.db")
        assert config.store_url == "sqlite://from-flag.db"
        assert config.store_source == "--store"

    @pytest.mark.usefixtures("clean_env")
    def test_the_project_key_beats_the_project_default(
        self, project: Path
    ) -> None:
        (project / "pyproject.toml").write_text(
            PYPROJECT + '\nstore = "sqlite://declared.db"\n'
        )
        config = ProjectConfig.discover(project)
        assert config.store_url == "sqlite://declared.db"
        assert config.store_source == "[tool.loom] store"

    @pytest.mark.usefixtures("clean_env")
    def test_a_malformed_pyproject_is_not_fatal(
        self, tmp_path: Path
    ) -> None:
        """Every command would otherwise fail on it, including ones not using it."""
        (tmp_path / "pyproject.toml").write_text("this is not [ toml")
        config = ProjectConfig.discover(tmp_path)
        assert config.root == tmp_path
        assert config.modules == []

    @pytest.mark.usefixtures("clean_env")
    def test_it_finds_a_project_from_a_subdirectory(
        self, project: Path
    ) -> None:
        nested = project / "a" / "b"
        nested.mkdir(parents=True)
        assert ProjectConfig.discover(nested).root == project


class TestRunsSurviveTheProcess:
    """The behaviour all of the above exists for."""

    def test_a_run_is_there_for_the_next_command(self, project: Path) -> None:
        started = json.loads(loom(project, "run", "keeper", "--json").stdout)
        listed = json.loads(loom(project, "runs", "--json").stdout)
        assert [row["run_id"] for row in listed] == [started["run_id"]]

    def test_show_finds_it(self, project: Path) -> None:
        started = json.loads(loom(project, "run", "keeper", "--json").stdout)
        done = loom(project, "show", started["run_id"], "--json")
        assert done.returncode == Exit.OK, done.stderr
        assert json.loads(done.stdout)["output"] == "kept"

    def test_the_store_file_lands_where_it_was_promised(self, project: Path) -> None:
        loom(project, "run", "keeper")
        assert (project / ".loom" / "runs.db").exists()

    @pytest.mark.usefixtures("clean_env")
    def test_a_directory_with_no_project_still_warns_on_detach(
        self, tmp_path: Path
    ) -> None:
        """An id for a run that will not exist is worse than no id.

        Looking one up afterwards reports no such run, which reads as the run
        having vanished rather than as never having been kept.
        """
        bare = tmp_path / "bare"
        bare.mkdir()
        (bare / "flows.py").write_text(FLOWS)
        done = loom(bare, "run", "flows.py::keeper", "--detach")
        assert done.returncode == Exit.OK, done.stderr
        assert "in memory" in done.stdout

    def test_a_project_does_not_warn(self, project: Path) -> None:
        assert "in memory" not in loom(project, "run", "keeper", "--detach").stdout


class TestAuthoredWorkflowsAreFindable:
    """The last step of the loop, which used to fail.

    ``loom author -o flows/digest.py`` wrote a file and ``loom run digest``
    then reported an unknown workflow, because a name resolves through
    ``[tool.loom] modules`` and nothing had added it — which reads as the
    authoring having failed rather than as a missing registration.
    """

    def _pyproject(self, project: Path, body: str) -> Path:
        (project / "pyproject.toml").write_text(body)
        module = project / "flows" / "new.py"
        module.parent.mkdir(exist_ok=True)
        module.write_text("")
        return module

    def test_a_single_line_list(self, project: Path) -> None:
        from loom.cli.config import ProjectConfig, register_module

        module = self._pyproject(project, PYPROJECT)
        assert register_module(project, module) == "added"
        assert "flows/new.py" in ProjectConfig.discover(project).modules

    def test_an_empty_list(self, project: Path) -> None:
        from loom.cli.config import ProjectConfig, register_module

        module = self._pyproject(
            project, '[project]\nname="x"\nversion="0.1"\n\n[tool.loom]\nmodules = []\n'
        )
        assert register_module(project, module) == "added"
        assert ProjectConfig.discover(project).modules == ["flows/new.py"]

    def test_a_multi_line_list(self, project: Path) -> None:
        from loom.cli.config import ProjectConfig, register_module

        module = self._pyproject(
            project,
            '[project]\nname="x"\nversion="0.1"\n\n[tool.loom]\nmodules = [\n    "a.py",\n]\n',
        )
        assert register_module(project, module) == "added"
        assert set(ProjectConfig.discover(project).modules) == {"a.py", "flows/new.py"}

    def test_a_section_with_no_modules_key(self, project: Path) -> None:
        from loom.cli.config import ProjectConfig, register_module

        module = self._pyproject(
            project,
            '[project]\nname="x"\nversion="0.1"\n\n[tool.loom]\nstore = "memory://"\n',
        )
        assert register_module(project, module) == "added"
        assert ProjectConfig.discover(project).modules == ["flows/new.py"]
        # The other key it found there survives.
        assert ProjectConfig.discover(project).store_url == "memory://"

    def test_no_section_at_all(self, project: Path) -> None:
        from loom.cli.config import ProjectConfig, register_module

        module = self._pyproject(project, '[project]\nname="x"\nversion="0.1"\n')
        assert register_module(project, module) == "added"
        assert ProjectConfig.discover(project).modules == ["flows/new.py"]

    def test_it_is_idempotent(self, project: Path) -> None:
        from loom.cli.config import register_module

        module = self._pyproject(project, PYPROJECT)
        assert register_module(project, module) == "added"
        assert register_module(project, module) == "present"

    def test_a_file_outside_the_project_is_left_alone(self, project: Path) -> None:
        """Where it lives is its own business, and a path that is not under the
        project has no relative form to record."""
        from loom.cli.config import register_module

        self._pyproject(project, PYPROJECT)
        assert register_module(project, Path("/tmp/elsewhere.py")) == "unchanged"

    def test_the_rest_of_the_file_survives(self, project: Path) -> None:
        from loom.cli.config import register_module

        body = PYPROJECT + '\n[tool.ruff]\nline-length = 100\n'
        module = self._pyproject(project, body)
        register_module(project, module)
        after = (project / "pyproject.toml").read_text()
        assert "[tool.ruff]" in after
        assert "line-length = 100" in after

    def test_a_mangled_edit_is_reverted(self, project: Path, monkeypatch) -> None:
        """This edits TOML as text. When the result does not parse the way it
        should, the file goes back rather than being left broken."""
        from loom.cli import config

        module = self._pyproject(project, PYPROJECT)
        before = (project / "pyproject.toml").read_text()
        monkeypatch.setattr(config, "_with_module", lambda *_: "this is not [ toml")
        assert config.register_module(project, module) == "unchanged"
        assert (project / "pyproject.toml").read_text() == before


class TestTheRunCommandIsSpelledOut:
    """A workflow's name comes from ``@workflow(name=...)`` and is routinely
    not the filename, so the hint used to read ``loom run <workflow>``
    literally — leaving the one thing the reader needs as the one thing it did
    not say."""

    def test_it_reads_the_declared_name(self, tmp_path: Path) -> None:
        from loom.cli.commands import declared_workflows

        path = tmp_path / "x.py"
        path.write_text(
            "from loom import Context, workflow\n\n"
            '@workflow(name="reverse_string_workflow")\n'
            "async def reverse_string(ctx: Context, s: str) -> str:\n"
            "    return s[::-1]\n"
        )
        assert declared_workflows(path) == ["reverse_string_workflow"]

    def test_a_bare_decorator_falls_back_to_the_function(self, tmp_path: Path) -> None:
        from loom.cli.commands import declared_workflows

        path = tmp_path / "x.py"
        path.write_text(
            "from loom import Context, workflow\n\n"
            "@workflow\n"
            "async def plain(ctx: Context) -> str:\n    return 'x'\n"
        )
        assert declared_workflows(path) == ["plain"]

    def test_it_never_imports_the_module(self, tmp_path: Path) -> None:
        """Running a freshly generated module's top level to find out what to
        call it is a side effect nobody asked for."""
        from loom.cli.commands import declared_workflows

        path = tmp_path / "x.py"
        path.write_text(
            "raise SystemExit('this module must never be executed')\n"
            "from loom import workflow\n"
        )
        assert declared_workflows(path) == []

    def test_unreadable_and_unparseable_are_empty(self, tmp_path: Path) -> None:
        from loom.cli.commands import declared_workflows

        broken = tmp_path / "broken.py"
        broken.write_text("def (:\n")
        assert declared_workflows(broken) == []
        assert declared_workflows(tmp_path / "absent.py") == []


class TestDotenv:
    """The CLI reads ``.env``; it used to be the cookbooks that did.

    A project with ``ANTHROPIC_API_KEY`` in ``.env`` ran under
    ``python examples/…`` and failed under ``loom author`` — and the failure
    message even acknowledged the asymmetry.
    """

    def test_it_loads_keys(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("LOOM_DOCTEST_KEY", raising=False)
        (tmp_path / "pyproject.toml").write_text(PYPROJECT)
        (tmp_path / ".env").write_text("LOOM_DOCTEST_KEY=from-file\n")
        assert load_dotenv(tmp_path) == tmp_path / ".env"
        assert os.environ["LOOM_DOCTEST_KEY"] == "from-file"
        monkeypatch.delenv("LOOM_DOCTEST_KEY", raising=False)

    def test_a_real_variable_wins(self, tmp_path: Path, monkeypatch) -> None:
        """Exporting a key for one command must still override the file."""
        monkeypatch.setenv("LOOM_DOCTEST_KEY", "from-shell")
        (tmp_path / ".env").write_text("LOOM_DOCTEST_KEY=from-file\n")
        load_dotenv(tmp_path)
        assert os.environ["LOOM_DOCTEST_KEY"] == "from-shell"

    def test_comments_and_blanks_are_skipped(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("LOOM_DOCTEST_PLAIN", raising=False)
        (tmp_path / ".env").write_text(
            "# a comment\n\nnot-an-assignment\nLOOM_DOCTEST_PLAIN =  bare  \n"
        )
        load_dotenv(tmp_path)
        assert os.environ["LOOM_DOCTEST_PLAIN"] == "bare"
        monkeypatch.delenv("LOOM_DOCTEST_PLAIN", raising=False)

    def test_quotes_preserve_whitespace(self, tmp_path: Path, monkeypatch) -> None:
        """Which is what quoting a value in a dotenv file is *for*."""
        monkeypatch.delenv("LOOM_DOCTEST_QUOTED", raising=False)
        (tmp_path / ".env").write_text("LOOM_DOCTEST_QUOTED='  spaced  '\n")
        load_dotenv(tmp_path)
        assert os.environ["LOOM_DOCTEST_QUOTED"] == "  spaced  "
        monkeypatch.delenv("LOOM_DOCTEST_QUOTED", raising=False)

    def test_no_env_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_dotenv(tmp_path) is None
        assert load_dotenv(None) is None

    def test_the_store_can_come_from_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LOOM_STORE", raising=False)
        (tmp_path / "pyproject.toml").write_text(PYPROJECT)
        (tmp_path / ".env").write_text("LOOM_STORE=sqlite://via-dotenv.db\n")
        config = ProjectConfig.discover(tmp_path)
        assert config.store_url == "sqlite://via-dotenv.db"
        # Attributed to the file, because "$LOOM_STORE" alone sends someone to
        # look at a shell that does not have it set.
        assert ".env" in config.store_source
        monkeypatch.delenv("LOOM_STORE", raising=False)


class TestDoctor:
    def test_a_healthy_project_exits_zero(self, project: Path) -> None:
        done = loom(project, "doctor")
        assert done.returncode == Exit.OK, done.stdout + done.stderr
        assert "ready" in done.stdout

    @pytest.mark.usefixtures("clean_env")
    def test_it_names_the_store_and_where_that_came_from(
        self, project: Path
    ) -> None:
        payload = json.loads(loom(project, "doctor", "--json").stdout)
        store = next(c for c in payload["checks"] if c["check"] == "store")
        assert ".loom/runs.db" in store["detail"]
        assert "project default" in store["detail"]

    def test_an_unreachable_store_fails(self, project: Path) -> None:
        """A URL that parses and a store that accepts a write are not the same."""
        done = loom(
            project, "doctor", "--store", "postgres://nobody@127.0.0.1:1/none"
        )
        assert done.returncode == Exit.FAILED
        payload = json.loads(
            loom(
                project,
                "doctor",
                "--store",
                "postgres://nobody@127.0.0.1:1/none",
                "--json",
            ).stdout
        )
        assert payload["ok"] is False
        assert any(c["check"] == "store-write" for c in payload["checks"])

    @pytest.mark.usefixtures("clean_env")
    def test_a_module_that_does_not_import_is_a_failure(
        self, project: Path
    ) -> None:
        """The failure most often mistaken for a missing workflow.

        ``resolve`` reports "no workflow named x" for it, which sends someone
        to check the name.
        """
        (project / "flows.py").write_text("import nonexistent_module_xyz\n")
        done = loom(project, "doctor")
        assert done.returncode == Exit.FAILED
        assert "does not import" in done.stdout

    def test_no_project_is_a_warning_not_a_failure(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        done = loom(bare, "doctor")
        assert done.returncode == Exit.OK
        assert "no pyproject.toml" in done.stdout


class TestScaffoldProducesAWorkingProject:
    def test_dry_run_and_write_agree(self, tmp_path: Path) -> None:
        """``--dry-run`` is worth nothing if it can disagree with the write."""
        from loom.cli.scaffold import scaffold_project, write_project

        planned = scaffold_project(str(tmp_path))
        written = write_project(str(tmp_path))
        assert sorted(planned) == sorted(written)

    def test_it_ignores_the_run_journal(self, tmp_path: Path) -> None:
        loom(tmp_path, "init", ".")
        assert ".loom/" in (tmp_path / ".gitignore").read_text()
        assert ".env" in (tmp_path / ".gitignore").read_text()

    def test_it_explains_where_a_key_goes(self, tmp_path: Path) -> None:
        loom(tmp_path, "init", ".")
        example = (tmp_path / ".env.example").read_text()
        assert "ANTHROPIC_API_KEY" in example
        assert "LOOM_STORE" in example

    def test_scaffolding_is_additive(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("mine\n")
        loom(tmp_path, "init", ".")
        assert (tmp_path / ".gitignore").read_text() == "mine\n"

    def test_the_scaffolded_project_persists_a_run(self, tmp_path: Path) -> None:
        """End to end: init, run, and find it again from a second process."""
        loom(tmp_path, "init", ".")
        started = json.loads(loom(tmp_path, "run", "quickstart", "-i", "alice", "--json").stdout)
        listed = json.loads(loom(tmp_path, "runs", "--json").stdout)
        assert [row["run_id"] for row in listed] == [started["run_id"]]
