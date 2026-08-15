import pytest


class TestWaitsDoNotHideDefects:
    """A long wait must not hide the code behind it.

    This is how unrunnable code got certified: a workflow opening with a long
    ``ctx.sleep`` suspends before any step executes, and suspended is not
    failed — so an unresolvable import in the first step went unnoticed and
    ``is_clean`` came back true. Failing the parked run instead was worse: the
    repair loop then pressured the model into deleting the wait. The sandbox
    fakes the clock, so the wait costs nothing and the steps still run.
    """

    def test_a_defect_behind_a_long_sleep_is_caught(self) -> None:
        from workflow_builder.agents.smoke import smoke_run

        code = '''
from datetime import timedelta
from workflow_builder import Context, step, workflow

@step
async def broken() -> str:
    """Import something that does not exist."""
    import definitely_not_a_real_module  # noqa: F401
    return "never"

@workflow(name="sleeper")
async def sleeper(ctx: Context, _in: str) -> str:
    await ctx.sleep(timedelta(minutes=4))
    return await ctx.step(broken)
'''
        result = smoke_run(code, workflow_input="x")

        # The four-minute sleep passes instantly and the step behind it runs,
        # so the real defect surfaces instead of being parked behind a timer.
        assert not result.ok
        assert "definitely_not_a_real_module" in result.error

    def test_a_long_sleep_alone_does_not_fail_a_good_workflow(self) -> None:
        """The wait is usually the point of the workflow; keeping it must be free."""
        from workflow_builder.agents.smoke import smoke_run

        code = '''
from datetime import timedelta
from workflow_builder import Context, step, workflow

@step
async def after_the_wait(name: str) -> str:
    """Real work, behind a long wait."""
    return name.upper()

@workflow(name="patient")
async def patient(ctx: Context, name: str) -> str:
    await ctx.sleep(timedelta(minutes=4))
    return await ctx.step(after_the_wait, name)
'''
        result = smoke_run(code, workflow_input="ada")

        assert result.ok, result.error
        assert result.status == "completed"
        assert result.steps_executed >= 1
        assert result.output_preview == "ADA"

    def test_parking_after_real_work_still_passes(self) -> None:
        """Human-in-the-loop workflows park by design; that is not a defect."""
        from workflow_builder.agents.smoke import smoke_run

        code = '''
from workflow_builder import Context, step, workflow

@step
async def prepare(name: str) -> str:
    """Do some real work before parking."""
    return name.upper()

@workflow(name="approver")
async def approver(ctx: Context, name: str) -> str:
    prepared = await ctx.step(prepare, name)
    if await ctx.wait_for_approval("release"):
        return prepared
    return "denied"
'''
        result = smoke_run(code, workflow_input="ada")

        assert result.ok, result.error
        assert result.status == "suspended"
        assert result.steps_executed >= 1

    def test_a_completing_workflow_reports_its_steps(self) -> None:
        from workflow_builder.agents.smoke import smoke_run

        code = '''
from workflow_builder import Context, step, workflow

@step
async def double(n: int) -> int:
    """Double it."""
    return n * 2

@workflow(name="doubler")
async def doubler(ctx: Context, n: int) -> int:
    return await ctx.step(double, n)
'''
        result = smoke_run(code, workflow_input=21)

        assert result.ok
        assert result.status == "completed"
        assert result.steps_executed >= 1


class TestEnvironmentalFailures:
    """A sandbox that cannot authenticate must not be read as broken code.

    Observed: the coding agent hit a 401 in the smoke run, was told to repair
    it, and complied by deleting the Gmail calls entirely. The stub passed smoke
    and came back ``is_clean``.
    """

    def test_auth_failures_are_environmental(self) -> None:
        from workflow_builder.agents.smoke import SmokeResult

        for message in (
            "Google API 401: invalid authentication credentials",
            "Request had insufficient authentication scopes",
            "Error code: 401 - API key is invalid",
            "httpx.ConnectError: All connection attempts failed",
        ):
            result = SmokeResult(ok=False, phase="run", error=message)
            assert result.environmental, message

    def test_code_defects_are_not_environmental(self) -> None:
        """These are exactly what the smoke run exists to catch."""
        from workflow_builder.agents.smoke import SmokeResult

        for message in (
            "No module named 'loom'",
            "cannot import name 'Retryy' from 'workflow_builder'",
            "TypeError: double() takes 1 positional argument but 2 were given",
        ):
            result = SmokeResult(ok=False, phase="run", error=message)
            assert not result.environmental, message

    def test_a_pass_is_never_environmental(self) -> None:
        from workflow_builder.agents.smoke import SmokeResult

        assert not SmokeResult(ok=True, phase="done").environmental

    async def test_the_repair_loop_leaves_environmental_failures_alone(self) -> None:
        """The regression: repairing an unfixable 401 costs the whole feature."""
        from workflow_builder.agents.coding_agent import WorkflowCodingAgent

        code = "# original code that needs credentials\n"
        calls: list[str] = []

        async def agent(_prompt: str) -> object:
            calls.append(_prompt)
            raise AssertionError("the model must not be asked to repair this")

        coder = WorkflowCodingAgent(object(), smoke_test=True)

        def fake_smoke(*_args, **_kwargs):
            from workflow_builder.agents.smoke import SmokeResult

            return SmokeResult(
                ok=False, phase="run", error="Google API 401: invalid credentials"
            )

        import workflow_builder.agents.coding_agent as module

        original = module.smoke_run
        module.smoke_run = fake_smoke
        try:
            result_code, smoke, rounds = await coder._repair_until_it_runs(agent, code)
        finally:
            module.smoke_run = original

        assert result_code == code, "the code was altered to satisfy a 401"
        assert rounds == 0
        assert calls == []
        assert smoke.environmental


class TestRepairBudget:
    """A repair that cannot be attempted must not discard working code.

    The repair call shares its turn budget with the generation that preceded
    it, so a long discovery phase leaves nothing for a repair round. Raising
    there threw away a candidate that may well have been fine.
    """

    async def test_an_exhausted_budget_keeps_the_code(self) -> None:
        from workflow_builder.agents.coding_agent import WorkflowCodingAgent
        from workflow_builder.core.exceptions import UsageLimitExceeded

        code = "# a candidate that was already produced\n"

        async def agent(_prompt: str) -> object:
            raise UsageLimitExceeded("agent exceeded its budget of 16 turns")

        coder = WorkflowCodingAgent(object(), smoke_test=True)

        def fake_smoke(*_args, **_kwargs):
            from workflow_builder.agents.smoke import SmokeResult

            return SmokeResult(ok=False, phase="run", error="TypeError: bad arity")

        import workflow_builder.agents.coding_agent as module

        original = module.smoke_run
        module.smoke_run = fake_smoke
        try:
            result_code, smoke, rounds = await coder._repair_until_it_runs(agent, code)
        finally:
            module.smoke_run = original

        assert result_code == code, "the candidate was discarded"
        assert not smoke.ok
        assert rounds == 1


class TestSmokeDerivesAnInput:
    """A typed input must not make correct code look broken.

    The smoke runner passed ``None`` unless a caller supplied ``smoke_input``,
    so a generated workflow annotated ``text: str`` crashed on its first
    attribute access. That marks the run failed, `is_clean` goes False, and the
    repair loop sets about "fixing" code that was right — the same
    environmental-failure trap that once returned a gutted stub, wearing a new
    costume.

    The fix reuses the fake generator that already builds values from a schema,
    so there is one answer to "what does a value of this shape look like".
    """

    HEAD = (
        "from workflow_builder import Context, step, workflow\n\n\n"
        "@step\n"
        "async def shout(text: str) -> str:\n"
        '    """Upper-case it."""\n'
        "    return text.upper()\n\n\n"
    )

    def _code(self, signature: str, call: str) -> str:
        return (
            f'{self.HEAD}@workflow(name="derived")\n'
            f"async def derived({signature}) -> str:\n"
            f'    """Shout it."""\n'
            f"    return await ctx.step(shout, {call})\n"
        )

    @pytest.mark.parametrize(
        ("signature", "call"),
        [
            ("ctx: Context, text: str", "text"),
            ("ctx: Context, n: int", "str(n)"),
            ("ctx: Context, data: dict", "str(data)"),
            ("ctx: Context", "'x'"),
        ],
        ids=["str", "int", "dict", "none"],
    )
    def test_a_declared_input_shape_is_supplied(
        self, signature: str, call: str
    ) -> None:
        from workflow_builder.agents.smoke import smoke_run

        result = smoke_run(self._code(signature, call))

        assert result.ok, f"{signature}: {result.error}"

    def test_an_explicit_input_still_wins(self) -> None:
        """Deriving is the fallback, not an override."""
        from workflow_builder.agents.smoke import smoke_run

        code = self._code("ctx: Context, text: str", "text")
        result = smoke_run(code, workflow_input="explicit")

        assert result.ok
        assert "EXPLICIT" in (result.output_preview or "")
