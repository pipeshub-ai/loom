"""An authoring job that outlives the process that started it.

``CodingSession`` held its transcript in a list and its budget in a dataclass,
and both died with the process. Ctrl+C four minutes into a generation discarded
the toolset schemas the model had fetched, the entity ids it had resolved
through real API calls, its plan, and every token paid for.

The load-bearing test here is :class:`TestASnapshotExistsMidLoop`. ``ask()`` is
one whole ReAct loop — twenty turns of discovery come back as a single call — so
snapshotting around it saves nothing until discovery has *finished*, which is
exactly the window an interruption lands in. The first version did that and
looked correct; only an interrupt at eleven seconds showed it saving nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom import Runtime
from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.generation import CodingSession, GenerationBudget
from loom.agents.messages import ToolCall, assistant, system, user
from loom.agents.session_store import (
    CodingSnapshot,
    StoreBackedSessionStore,
    new_session_id,
)
from loom.core.models import Usage
from loom.facade import LocalFacade
from loom.stores import MemoryStore
from loom.testing.mock import MockModelProvider, mock_response

WORKFLOW = (
    "from loom import Context, workflow\n\n\n"
    '@workflow(name="resumed")\n'
    'async def resumed(ctx: Context, _: str = "") -> str:\n'
    '    """Something that compiles."""\n'
    '    return "ok"\n'
)


def scripted(*, tools: int = 1) -> MockModelProvider:
    responses = [
        mock_response(
            tool_calls=[
                ToolCall(id=str(n), name="search_toolsets", arguments={"query": "x"})
            ]
        )
        for n in range(tools)
    ]
    responses.append(
        mock_response(
            tool_calls=[
                ToolCall(
                    id="final",
                    name="final_output",
                    arguments={"code": WORKFLOW, "explanation": "done", "plan": []},
                )
            ]
        )
    )
    return MockModelProvider(responses=responses)


@pytest.fixture
def sessions() -> StoreBackedSessionStore:
    return StoreBackedSessionStore(MemoryStore())


class TestTheSnapshot:
    def test_it_round_trips(self) -> None:
        snapshot = CodingSnapshot(
            session_id="auth_x",
            spec="do a thing",
            history=[user("hi"), assistant("ok")],
            spent=Usage(input_tokens=10, output_tokens=5),
            turns_used=2,
            code=WORKFLOW,
        )
        back = CodingSnapshot.from_json(snapshot.as_json())
        assert back.session_id == "auth_x"
        assert [m.role.value for m in back.history] == ["user", "assistant"]
        assert back.spent.input_tokens == 10
        assert back.turns_used == 2
        assert back.code == WORKFLOW

    def test_tool_calls_survive(self) -> None:
        """The expensive half: what the model looked up, and what came back."""
        message = assistant(
            None, tool_calls=[ToolCall(id="1", name="search_toolsets", arguments={"q": "jira"})]
        )
        back = CodingSnapshot.from_json(
            CodingSnapshot(session_id="a", spec="", history=[message]).as_json()
        )
        assert back.history[0].tool_calls[0].name == "search_toolsets"
        assert back.history[0].tool_calls[0].arguments == {"q": "jira"}

    async def test_it_persists_and_loads(self, sessions) -> None:
        snapshot = CodingSnapshot(session_id=new_session_id(), spec="a spec")
        await sessions.save(snapshot)
        assert (await sessions.load(snapshot.session_id)).spec == "a spec"

    async def test_an_unknown_id_is_none(self, sessions) -> None:
        assert await sessions.load("auth_nope") is None

    async def test_recent_is_newest_first(self, sessions) -> None:
        ids = [new_session_id() for _ in range(3)]
        for session_id in ids:
            await sessions.save(CodingSnapshot(session_id=session_id, spec=session_id))
        assert [s.session_id for s in await sessions.recent()] == list(reversed(ids))

    async def test_saving_twice_does_not_duplicate(self, sessions) -> None:
        snapshot = CodingSnapshot(session_id="auth_one", spec="x")
        await sessions.save(snapshot)
        await sessions.save(snapshot)
        assert [s.session_id for s in await sessions.recent()] == ["auth_one"]


class TestSessionRestore:
    def _session(self, **kwargs: Any) -> CodingSession:
        return CodingSession(
            agent=object(), budget=GenerationBudget(max_turns=10), **kwargs
        )

    def test_the_transcript_comes_back(self) -> None:
        session = self._session()
        session.restore(
            CodingSnapshot(session_id="a", spec="s", history=[user("hi")])
        )
        assert [m.text() for m in session.history] == ["hi"]

    def test_what_it_cost_comes_back_too(self) -> None:
        """A resumed job handed a fresh budget would let an interrupted run be
        restarted indefinitely under a ceiling meant to bound it."""
        session = self._session()
        session.restore(
            CodingSnapshot(
                session_id="a",
                spec="s",
                spent=Usage(input_tokens=900, output_tokens=100),
                turns_used=4,
            )
        )
        assert session.budget.spent.total_tokens == 1000
        assert session.budget.turns_used == 4

    def test_a_restored_budget_can_already_be_exhausted(self) -> None:
        session = CodingSession(
            agent=object(),
            budget=GenerationBudget(max_turns=10, max_total_tokens=500),
        )
        session.restore(
            CodingSnapshot(
                session_id="a", spec="s", spent=Usage(input_tokens=900)
            )
        )
        assert session.budget.exhausted

    def test_the_system_prompt_is_never_stored(self) -> None:
        """The runner prepends it on every call, so a stored one would be sent
        twice and grow by one each round."""
        session = self._session()
        session.restore(
            CodingSnapshot(
                session_id="a", spec="s", history=[system("rules"), user("hi")]
            )
        )
        # Restore keeps what it was given; what matters is that nothing *puts*
        # a system message in, which the turn watcher below asserts.
        assert any(m.role.value == "user" for m in session.history)


class TestASnapshotExistsMidLoop:
    """The defect that only an interrupt could find.

    ``ask()`` returns once, after the whole ReAct loop. Persisting around it
    means nothing is saved until discovery is over — and discovery is the
    window an interruption lands in.
    """

    async def test_a_snapshot_lands_before_the_loop_returns(
        self, sessions
    ) -> None:
        seen: list[int] = []
        original = sessions.save

        async def counting(snapshot: CodingSnapshot) -> None:
            seen.append(len(snapshot.history))
            await original(snapshot)

        sessions.save = counting  # type: ignore[method-assign]
        agent = WorkflowCodingAgent(
            scripted(tools=3), smoke_test=False, session_store=sessions
        )
        await agent.generate("do a thing")

        # Three tool turns plus the final one: more than one save, which is
        # what says a snapshot existed *during* the loop rather than only at
        # the end of it.
        assert len(seen) > 1, f"only {len(seen)} snapshot(s) — none mid-loop"

    async def test_the_transcript_grows(self, sessions) -> None:
        agent = WorkflowCodingAgent(
            scripted(tools=2), smoke_test=False, session_store=sessions
        )
        await agent.generate("do a thing")
        snapshot = await sessions.load(agent.session_id)
        assert snapshot is not None
        assert snapshot.history, "a snapshot with no transcript resumes nothing"
        assert all(m.role.value != "system" for m in snapshot.history)

    async def test_no_store_writes_nothing(self) -> None:
        """An agent given no session store runs exactly as it did before."""
        agent = WorkflowCodingAgent(scripted(), smoke_test=False)
        assert (await agent.generate("do a thing")).code
        assert agent.session_id == ""
        assert agent.resumable is False


class TestResumableIsHonest:
    """Offering an id that resolves to nothing is the advice-that-cannot-help
    failure, which this CLI has fixed twice already."""

    async def test_not_resumable_before_any_turn(self, sessions) -> None:
        agent = WorkflowCodingAgent(
            scripted(), smoke_test=False, session_store=sessions
        )
        assert agent.resumable is False

    async def test_resumable_after_a_turn(self, sessions) -> None:
        agent = WorkflowCodingAgent(
            scripted(), smoke_test=False, session_store=sessions
        )
        await agent.generate("do a thing")
        assert agent.resumable is True

    async def test_the_id_exists_before_the_job_runs(self, sessions) -> None:
        """It is useless to somebody who only learns it from a result they
        never got."""
        agent = WorkflowCodingAgent(
            scripted(), smoke_test=False, session_store=sessions
        )
        assert agent.session_id.startswith("auth_")


class TestResumingKeepsTheId:
    async def test_a_resumed_job_reuses_it(self, sessions) -> None:
        """A fresh id per attempt would leave a trail of half-finished
        snapshots and no way to tell which continues which."""
        first = CodingSnapshot(session_id="auth_keep", spec="s")
        await sessions.save(first)
        agent = WorkflowCodingAgent(
            scripted(), smoke_test=False, session_store=sessions, resume=first
        )
        assert agent.session_id == "auth_keep"


class TestThroughTheFacade:
    async def test_the_id_is_reported(self) -> None:
        facade = LocalFacade(Runtime(store=MemoryStore()))
        agent = await facade._coding_agent(
            packages=None, smoke_input=None, observe=False
        )
        assert agent.session_id
        assert facade.last_session_id == agent.session_id

    async def test_an_unknown_id_is_refused_by_name(self) -> None:
        from loom.core.exceptions import RegistryError

        facade = LocalFacade(Runtime(store=MemoryStore()))
        with pytest.raises(RegistryError, match="no authoring session"):
            await facade._coding_agent(
                packages=None, smoke_input=None, observe=False, resume="auth_nope"
            )

    async def test_resume_crosses_the_port(self) -> None:
        import inspect

        from loom.facade import RemoteFacade, RuntimeFacade
        from loom.identity.facade import AuthorizedFacade

        for adapter in (RuntimeFacade, LocalFacade, RemoteFacade, AuthorizedFacade):
            assert "resume" in inspect.signature(adapter.author).parameters
