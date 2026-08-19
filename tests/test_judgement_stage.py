"""A model call doing work a rule would have done.

The generation this was written for answered its spec correctly, which is why
nothing caught it. Asked to list tickets past their due date, it fetched the
rows into typed models — every field it needed already on them — and then
handed the rows to ``ctx.agent()`` to "fetch the details and produce a markdown
table". The table was right. What it cost is invisible in the file: a model
request on every run, and an answer re-derived each time from rows the code was
holding all along.

``CoverageStage`` and ``OutcomeStage`` set the discipline this follows — two
conditions have to coincide before it says anything, because the same shape is
correct whenever the spec asked for judgement.
"""

from __future__ import annotations

from loom.agents.checks import CheckContext
from loom.agents.stages import JudgementStage, default_stages

DATA_SPEC = "list the tickets past their due date, include the due date"


async def check(code: str, spec: str = DATA_SPEC):
    return await JudgementStage().run(code, CheckContext(spec=spec))


def workflow(body: str) -> str:
    return (
        "from loom import Context, workflow\n\n\n"
        '@workflow(name="w")\n'
        "async def w(ctx: Context, data) -> str:\n" + body
    )


class TestTheAnswerComesOutOfAModel:
    async def test_the_generation_that_prompted_it(self) -> None:
        code = workflow(
            '    rows = await ctx.step(search, "duedate < now()")\n'
            '    report = await ctx.agent(f"make a table of {rows}")\n'
            "    return report.text()\n"
        )

        issues = (await check(code)).issues

        assert len(issues) == 1
        assert issues[0].category == "judgement"
        assert issues[0].severity == "error", "a warning drives no repair"

    async def test_an_f_string_around_the_model_output_counts(self) -> None:
        """Wrapping the answer in a heading does not make the answer the code's."""
        code = workflow(
            '    verdict = await ctx.agent("write it up")\n'
            '    return f"## Report\\n{verdict.text()}"\n'
        )

        assert (await check(code)).issues

    async def test_the_inline_form_counts(self) -> None:
        code = workflow('    return (await ctx.agent("do it")).text()\n')

        assert (await check(code)).issues

    async def test_it_names_the_line_and_the_way_out(self) -> None:
        code = workflow(
            '    answer = await ctx.agent("go")\n    return answer.text()\n'
        )

        result = await check(code)

        assert result.detail, "the lines, for anything that wants to point at them"
        message = result.issues[0].message
        assert "@step" in message, "say what to write instead"
        assert "return the code unchanged" in message, "and how to decline"


class TestWhatItMustNotFlag:
    async def test_a_spec_that_asks_for_judgement(self) -> None:
        """The same code, a different request, and now it is the right design."""
        code = workflow(
            '    rows = await ctx.step(search, "x")\n'
            '    out = await ctx.agent(f"summarise {rows}")\n'
            "    return out.text()\n"
        )

        result = await check(code, "summarise my overdue tickets")

        assert not result.issues
        assert result.reason, "and it says why it stayed quiet"

    async def test_a_model_call_that_is_not_the_answer(self) -> None:
        """Judgement mid-flow — the resolution ladder's own rung 3."""
        code = workflow(
            '    which = await ctx.agent("which project is meant?")\n'
            "    rows = await ctx.step(search, which.text())\n"
            "    return await ctx.step(fmt, rows)\n"
        )

        assert not (await check(code)).issues

    async def test_a_step_composing_after_a_judgement(self) -> None:
        """The split the prompt asks for: a model decides, a step composes.

        Following the value naively would report this as the failure it is the
        fix for — the model's output *is* in the returned expression, and it
        has been through a step, which is where the answer was produced.
        """
        code = workflow(
            '    verdict = await ctx.agent("which of these matter?")\n'
            "    return await ctx.step(render, verdict.text())\n"
        )

        assert not (await check(code)).issues

    async def test_a_node_composing_after_a_judgement(self) -> None:
        """A catalogued node is code with a contract, same as a step."""
        code = workflow(
            '    verdict = await ctx.agent("which?")\n'
            '    return await ctx.node("transform.template", verdict.text())\n'
        )

        assert not (await check(code)).issues

    async def test_a_workflow_with_no_model_call(self) -> None:
        code = workflow(
            '    rows = await ctx.step(search, "x")\n'
            "    return await ctx.step(fmt, rows)\n"
        )

        assert not (await check(code)).issues

    async def test_a_helper_that_returns_model_output(self) -> None:
        """What matters is the *run's* answer, not any function's."""
        code = (
            "async def helper(ctx):\n"
            '    return await ctx.agent("x")\n\n\n'
            + workflow("    return await ctx.step(fmt, 1)\n")
        )

        assert not (await check(code)).issues

    async def test_no_spec_means_no_opinion(self) -> None:
        """Intent is read from the spec; without one there is nothing to read."""
        code = workflow('    return (await ctx.agent("go")).text()\n')

        result = await check(code, "")

        assert result.skipped and result.reason

    async def test_unparseable_code_is_not_this_stage_s_problem(self) -> None:
        assert not (await check("def (")).issues


class TestWhereItSits:
    def test_it_is_non_blocking_and_static(self) -> None:
        """Nothing after it depends on the answer, and it costs an AST walk."""
        assert JudgementStage().blocking is False

        names = [stage.name for stage in default_stages()]
        assert names.index("judgement") < names.index("smoke")
