"""Every agent is told what day it is, and reads it from the same clock.

A model's sense of "now" is the end of its training data, and it has no way to
notice that the sense is stale. Asked for a workflow reporting the winner of a
tournament held three months earlier, the coding agent wrote a refusal into the
file where the workflow should have been — the season "hasn't happened yet" —
and every verification stage passed it, because the code compiled, ran, and
answered. Nothing downstream can catch that: it is a considered explanation,
not a defect in the generated code.

So these are wiring tests first and rendering tests second, for the reason
``test_ask_user_wiring.py`` gives: the renderer can be complete and correct
while no caller passes it in, and a module's own unit tests cannot tell the
difference because they construct the thing themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from loom import Runtime
from loom.agents.agent import Agent
from loom.agents.backend import BuiltInBackend
from loom.agents.coding_agent import AUTHORING_TIME_NOTE, WorkflowCodingAgent
from loom.agents.now import local_timezone, time_block, timezone_label
from loom.facade import LocalFacade
from loom.runtime.clock import ManualClock
from loom.testing.mock import MockModelProvider, mock_response

#: A moment with something to say: a Friday, a month whose name is not its
#: number, and an hour that lands on the previous day in a western zone.
MOMENT = datetime(2026, 8, 21, 2, 30, tzinfo=UTC)


def _clock() -> ManualClock:
    return ManualClock(MOMENT)


@pytest.fixture(autouse=True)
def _fixed_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the reported zone for the whole module.

    Without it every assertion here is a statement about the machine the suite
    runs on: 02:30 UTC is the 20th in Los Angeles and the 21st in Kolkata, so
    the same correct code passes or fails by geography. The zone ladder itself
    is tested by overriding this, below.
    """
    monkeypatch.setenv("LOOM_TIMEZONE", "UTC")


class TestWhatItSays:
    def test_the_date_is_spelled_out_and_dated(self) -> None:
        block = time_block(_clock(), tz=UTC)
        assert "Friday 21 August 2026" in block
        assert "2026-08-21" in block

    def test_the_time_comes_from_the_clock_not_the_wall(self) -> None:
        """The point of the port: a test pins what the agent is told.

        Reading ``datetime.now()`` here would make every assertion above a
        statement about the day the suite happens to run.
        """
        assert "2026" in time_block(_clock(), tz=UTC)
        assert "1999" in time_block(ManualClock(datetime(1999, 12, 31, tzinfo=UTC)), tz=UTC)

    def test_the_local_time_and_the_utc_time_are_both_there(self) -> None:
        """Two clocks, because either alone is ambiguous.

        02:30 UTC is the 20th in Los Angeles. A block naming one date without
        the offset that produced it cannot be checked against anything.
        """
        block = time_block(_clock(), tz=ZoneInfo("America/Los_Angeles"))
        assert "Thursday 20 August 2026, 19:30" in block
        assert "2026-08-21 02:30" in block

    def test_it_says_the_training_data_is_older_than_this(self) -> None:
        """The date alone does not fix the failure.

        A model that reads the date and still reasons from memory about what
        has happened writes the same refusal. The block has to say which of
        the two to trust.
        """
        block = time_block(_clock(), tz=UTC).lower()
        assert "training data" in block
        assert "look it up" in block

    def test_a_note_is_appended_when_given_and_absent_when_not(self) -> None:
        assert "PIN" in time_block(_clock(), tz=UTC, note="PIN")
        assert "PIN" not in time_block(_clock(), tz=UTC)


class TestTheZone:
    def test_a_named_zone_carries_its_name_and_its_offset(self) -> None:
        """Both halves. ``IST`` is Indian, Irish and Israeli Standard Time, and
        an offset alone cannot say which side of a DST change a future date
        falls on."""
        label = timezone_label(ZoneInfo("Asia/Kolkata"), MOMENT)
        assert label == "Asia/Kolkata (UTC+05:30)"

    def test_a_western_offset_is_signed(self) -> None:
        label = timezone_label(ZoneInfo("America/Los_Angeles"), MOMENT)
        assert label == "America/Los_Angeles (UTC-07:00)"

    def test_utc_is_not_named_twice(self) -> None:
        assert timezone_label(UTC, MOMENT) == "UTC+00:00"

    def test_loom_timezone_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """For a deployment whose host clock is UTC and whose users are not."""
        monkeypatch.setenv("LOOM_TIMEZONE", "Asia/Kolkata")
        monkeypatch.setenv("TZ", "America/New_York")
        assert getattr(local_timezone(), "key", "") == "Asia/Kolkata"

    def test_tz_is_read_when_loom_timezone_is_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LOOM_TIMEZONE", raising=False)
        monkeypatch.setenv("TZ", "Asia/Kolkata")
        assert getattr(local_timezone(), "key", "") == "Asia/Kolkata"

    def test_a_typo_falls_through_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Somebody's misspelled ``$TZ`` is not a reason to fail an agent run:
        the next candidate is still better than nothing."""
        monkeypatch.setenv("LOOM_TIMEZONE", "Mars/Olympus_Mons")
        monkeypatch.setenv("TZ", "also nonsense")
        assert local_timezone() is not None
        assert "Friday 21 August 2026" in time_block(_clock(), tz=UTC)


class TestTheCodingAgentIsToldFirst:
    """The surface the failure was observed on."""

    def _agent(self, **kwargs: object) -> WorkflowCodingAgent:
        return WorkflowCodingAgent(
            MockModelProvider(), clock=_clock(), smoke_test=False, **kwargs
        )

    def test_the_system_prompt_carries_the_date(self) -> None:
        assert "Friday 21 August 2026" in self._agent().build_system_prompt()

    def test_custom_instructions_do_not_replace_it(self) -> None:
        """``instructions=`` replaces the *instructions*, and what day it is
        has never been one of them — the position the package list and the
        toolset docs already take."""
        prompt = self._agent(instructions="You write workflows.").build_system_prompt()
        assert "You write workflows." in prompt
        assert "Friday 21 August 2026" in prompt

    def test_it_says_which_half_of_the_date_belongs_in_the_code(self) -> None:
        """A date in the prompt is an invitation to write it into the file, and
        a workflow with today's date frozen in it is wrong on every run after
        this one."""
        prompt = self._agent().build_system_prompt()
        assert AUTHORING_TIME_NOTE in prompt
        assert "ctx.now()" in AUTHORING_TIME_NOTE

    def test_without_a_clock_it_still_says_something(self) -> None:
        """No clock is the wall clock, not silence."""
        prompt = WorkflowCodingAgent(MockModelProvider(), smoke_test=False).build_system_prompt()
        assert "## Current date and time" in prompt


class TestTheFacadeHandsOverTheRuntimesClock:
    """A Runtime under a fixed clock and an authoring job reading the wall
    clock would disagree about the date in the one place that has to agree."""

    @pytest.mark.asyncio
    async def test_author_reads_runtime_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        facade = LocalFacade(Runtime(clock=_clock()))
        agent = await facade._coding_agent(
            packages=None, smoke_input=None, observe=False
        )
        assert "Friday 21 August 2026" in agent.build_system_prompt()


class TestEveryOtherAgentToo:
    """``ctx.agent()`` has the same problem, so it gets the same block."""

    async def _system_prompt(self, agent: Agent[object], **kwargs: object) -> str:
        model = agent.model
        assert isinstance(model, MockModelProvider)
        await agent("hello", **kwargs)  # type: ignore[arg-type]
        request = model.last_request()
        assert request is not None
        first = request.messages[0]
        assert first.role.value == "system"
        return str(first.content or "")

    @pytest.mark.asyncio
    async def test_the_turn_loop_puts_it_in_the_system_message(self) -> None:
        from loom.agents.executor import AgentContext

        agent: Agent[object] = Agent(
            name="t",
            instructions="Be helpful.",
            model=MockModelProvider(responses=[mock_response("ok")]),
        )
        prompt = await self._system_prompt(
            agent, context=AgentContext(clock=_clock())
        )
        assert "Be helpful." in prompt
        assert "Friday 21 August 2026" in prompt

    @pytest.mark.asyncio
    async def test_an_agent_with_no_instructions_still_gets_one(self) -> None:
        """There was no system message at all before; now there is one, and it
        is the date."""
        from loom.agents.executor import AgentContext

        agent: Agent[object] = Agent(
            name="t", model=MockModelProvider(responses=[mock_response("ok")])
        )
        prompt = await self._system_prompt(
            agent, context=AgentContext(clock=_clock())
        )
        assert "Friday 21 August 2026" in prompt

    @pytest.mark.asyncio
    async def test_time_aware_false_is_exactly_what_shipped_before(self) -> None:
        """For an agent whose job cannot turn on the date."""
        model = MockModelProvider(responses=[mock_response("ok")])
        agent: Agent[object] = Agent(
            name="t", instructions="Classify.", model=model, time_aware=False
        )
        await agent("hello")
        request = model.last_request()
        assert request is not None
        assert str(request.messages[0].content) == "Classify."

    @pytest.mark.asyncio
    async def test_the_default_backend_threads_its_clock(self) -> None:
        """``ctx.agent("prompt")`` goes through a backend rather than through
        ``run_agent_durably``, and the two must be told the same time."""
        model = MockModelProvider(responses=[mock_response("ok")])
        await BuiltInBackend(model=model, clock=_clock()).run("hello")
        request = model.last_request()
        assert request is not None
        assert "Friday 21 August 2026" in str(request.messages[0].content)


class TestInsideAWorkflow:
    @pytest.mark.asyncio
    async def test_an_agent_in_a_body_reads_the_engines_clock(self) -> None:
        """Under ``ManualClock`` the agent is told the moment the test chose,
        not the moment the test runs."""
        from loom import workflow
        from loom.stores.memory import MemoryStore

        model = MockModelProvider(responses=[mock_response("ok")])

        agent: Agent[object] = Agent(name="asker", instructions="Answer.", model=model)

        @workflow(name="asks")
        async def asks(ctx: object) -> str:
            result = await ctx.agent(agent, "what year is it")  # type: ignore[attr-defined]
            return str(result.output)

        rt = Runtime(store=MemoryStore(), clock=_clock())
        rt.register(asks)
        await rt.run(asks)

        request = model.last_request()
        assert request is not None
        assert "Friday 21 August 2026" in str(request.messages[0].content)
