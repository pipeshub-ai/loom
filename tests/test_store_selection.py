"""Persistence is a deployment decision, not a workflow's.

The coding agent used to bake ``Runtime(store=MemoryStore())`` into every file it
generated, so each workflow chose its own persistence and could not be moved to
Postgres without editing it. These tests pin the corrected layering: a workflow
module declares steps and workflows, and the *caller* supplies the store.
"""

from __future__ import annotations

import pytest

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT
from loom.agents.validator import CodeValidator
from loom.core.exceptions import ConfigurationError
from loom.stores import from_url, store_from_env
from loom.stores.factory import STORE_URL_ENV
from loom.stores.memory import MemoryStore
from loom.stores.sqlite import SQLiteStore


@step
async def double(n: int) -> int:
    """Double a number."""
    return n * 2


@workflow(name="portable")
async def portable(ctx: Context, n: int) -> int:
    """Says nothing about where its journal lives."""
    return await ctx.step(double, n)


# ---------------------------------------------------------------------------
# The store factory
# ---------------------------------------------------------------------------


class TestStoreFactory:
    def test_memory_url(self) -> None:
        assert isinstance(from_url("memory://"), MemoryStore)

    def test_bare_scheme_defaults_to_memory(self) -> None:
        assert isinstance(from_url(""), MemoryStore)

    def test_sqlite_in_memory(self) -> None:
        assert isinstance(from_url("sqlite://"), SQLiteStore)

    def test_sqlite_relative_path(self) -> None:
        store = from_url("sqlite://runs.db")
        assert isinstance(store, SQLiteStore)
        assert store.path == "runs.db"

    def test_sqlite_absolute_path(self, tmp_path) -> None:
        target = tmp_path / "runs.db"
        store = from_url(f"sqlite://{target}")
        assert store.path == str(target)

    def test_unknown_scheme_names_the_alternatives(self) -> None:
        with pytest.raises(ConfigurationError) as caught:
            from_url("cassandra://localhost")

        message = str(caught.value)
        assert "cassandra" in message
        assert "sqlite" in message and "postgres" in message

    def test_env_var_selects_the_store(self, monkeypatch) -> None:
        monkeypatch.setenv(STORE_URL_ENV, "sqlite://from-env.db")
        assert store_from_env().path == "from-env.db"

    def test_env_var_absent_falls_back(self, monkeypatch) -> None:
        monkeypatch.delenv(STORE_URL_ENV, raising=False)
        assert isinstance(store_from_env(), MemoryStore)


class TestRuntimeFromEnv:
    async def test_defaults_to_memory(self, monkeypatch) -> None:
        monkeypatch.delenv(STORE_URL_ENV, raising=False)
        rt = Runtime.from_env()

        assert isinstance(rt.store, MemoryStore)
        assert (await rt.run(portable, 4)).output == 8

    async def test_env_var_changes_the_store_not_the_workflow(
        self, monkeypatch, tmp_path
    ) -> None:
        """The same workflow object, a different backend, no code change."""
        monkeypatch.setenv(STORE_URL_ENV, f"sqlite://{tmp_path / 'runs.db'}")
        rt = Runtime.from_env()
        try:
            assert isinstance(rt.store, SQLiteStore)
            result = await rt.run(portable, 4)

            assert result.status is ExecutionStatus.COMPLETED
            assert result.output == 8
            # It really persisted: the record survives a fresh Runtime.
            reopened = Runtime(store=rt.store)
            assert (await reopened.get(result.run_id)) is not None
        finally:
            await rt.store.close()

    async def test_other_arguments_still_apply(self, monkeypatch) -> None:
        monkeypatch.delenv(STORE_URL_ENV, raising=False)
        rt = Runtime.from_env(node_id="fixed", journal_max_entries=7)

        assert rt.node_id == "fixed"
        assert rt.journal_max_entries == 7

    async def test_an_explicit_store_wins_over_the_environment(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(STORE_URL_ENV, "sqlite://ignored.db")
        rt = Runtime.from_env(store=MemoryStore())

        assert isinstance(rt.store, MemoryStore)


class TestWorkflowPortability:
    @pytest.mark.parametrize("url", ["memory://", "sqlite://"])
    async def test_one_workflow_runs_against_every_store(self, url: str) -> None:
        """The point of the whole change."""
        store = from_url(url)
        rt = Runtime(store=store)
        try:
            assert (await rt.run(portable, 21)).output == 42
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                await close()


# ---------------------------------------------------------------------------
# The coding agent must not choose one either
# ---------------------------------------------------------------------------


class TestGeneratedCodeDoesNotChooseAStore:
    def test_the_prompt_does_not_mandate_a_store_import(self) -> None:
        assert "from loom.stores.memory import MemoryStore" not in (
            DEFAULT_SYSTEM_PROMPT
        )

    def test_the_prompt_forbids_choosing_one(self) -> None:
        # Keyword, not a sentence: the wording is edited often, the rule is not.
        collapsed = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "Never import a store" in collapsed
        assert "host's choice" in collapsed

    def test_the_prompt_points_at_the_environment(self) -> None:
        assert "Runtime.from_env()" in DEFAULT_SYSTEM_PROMPT

    def test_module_level_store_is_flagged(self) -> None:
        code = '''
from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore

rt = Runtime(store=MemoryStore())

@step
async def s() -> int:
    """A step."""
    return 1

@workflow(name="w")
async def w(ctx: Context, x: int) -> int:
    return await ctx.step(s)
'''
        issues = CodeValidator().validate(code)
        messages = [i.message for i in issues if "MemoryStore" in i.message]

        assert messages
        assert "should not bind its own store" in messages[0]

    def test_a_store_inside_a_function_is_allowed(self) -> None:
        """Inside a function the choice is made by whoever calls it."""
        code = '''
from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore

@step
async def s() -> int:
    """A step."""
    return 1

@workflow(name="w")
async def w(ctx: Context, x: int) -> int:
    return await ctx.step(s)

async def main() -> None:
    rt = Runtime(store=MemoryStore())
    await rt.run(w, 1)
'''
        assert CodeValidator().validate(code) == []

    def test_a_store_under_main_is_allowed(self) -> None:
        """A demo block is a script, not the library."""
        code = '''
from loom import Context, step, workflow

@step
async def s() -> int:
    """A step."""
    return 1

@workflow(name="w")
async def w(ctx: Context, x: int) -> int:
    return await ctx.step(s)

if __name__ == "__main__":
    import asyncio

    from loom import Runtime
    from loom.stores.memory import MemoryStore

    asyncio.run(Runtime(store=MemoryStore()).run(w, 1))
'''
        assert CodeValidator().validate(code) == []

    def test_the_recommended_shape_validates_clean(self) -> None:
        code = '''
from loom import Context, Retry, step, workflow


@step(retry=Retry(max_attempts=3))
async def fetch(key: str) -> dict:
    """Fetch a record."""
    return {"key": key}


@workflow(name="lookup")
async def lookup(ctx: Context, key: str) -> dict:
    """Fetch a record durably."""
    return await ctx.step(fetch, key)


if __name__ == "__main__":
    import asyncio

    from loom import Runtime

    async def main() -> None:
        result = await Runtime.from_env().run(lookup, "abc")
        print(f"{result.status.value}: {result.output}")

    asyncio.run(main())
'''
        assert CodeValidator().validate(code) == []

    def test_the_scaffold_template_follows_its_own_advice(self) -> None:
        """`loom init` writes the shape the prompt describes."""
        from loom.cli.scaffold import QUICKSTART_WORKFLOW

        assert "Runtime.from_env()" in QUICKSTART_WORKFLOW
        assert CodeValidator().validate(QUICKSTART_WORKFLOW) == []
