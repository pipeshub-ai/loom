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


class TestAModelDecisionActedOnIsNotTheAnswer:
    """The dual of the laundering rule, and the false positive without it.

    ``is_laundered`` says a value coming *out* of a durable call is the code's,
    whatever model input went in. The missing half was the value going *in*: a
    name an ambiguous-resolution agent chose, handed to ``ctx.step`` to fetch
    with, is a model deciding and a step answering — the split this stage
    exists to protect.

    It fired anyway when the resolved key was also echoed above the table, and
    that mattered more than a stray warning would: ``ResolutionStage``
    *instructs* this shape ("if it stays ambiguous, resolve it in a ctx.agent()
    step with the candidates"), so two checks disagreed about the same correct
    code and the repair loop had no move that satisfied both.
    """

    LADDER = (
        '    found = await ctx.step(resolve_epic, "saas")\n'
        '    picked = await ctx.agent(f"which epic? {found.note}")\n'
        "    key = picked.text().strip()\n"
        '    rows = await ctx.step(search, f"parentEpic = {key}")\n'
        '    return f"# {key}\\n" + "\\n".join(r.summary for r in rows)\n'
    )

    async def test_a_resolution_the_code_fetched_with_is_left_alone(self) -> None:
        assert not (await check(workflow(self.LADDER))).issues

    async def test_echoing_the_resolved_value_in_the_answer_is_still_fine(
        self,
    ) -> None:
        """Labelling a table with what was resolved is not producing it."""
        code = workflow(
            '    picked = await ctx.agent("which epic?")\n'
            "    key = picked.text()\n"
            '    rows = await ctx.step(search, key)\n'
            "    lines = [f\"# {key}\"]\n"
            "    for row in rows:\n"
            '        lines.append(row.summary)\n'
            '    return "\\n".join(lines)\n'
        )

        assert not (await check(code)).issues

    async def test_binding_the_query_first_reads_the_same_as_inlining_it(
        self,
    ) -> None:
        """The verdict must not turn on where the model put a newline.

        Consumption follows the same bindings taint does, or one intermediate
        name hides it: ``ctx.step(search, f"... {key}")`` consumes ``key``
        while ``jql = f"... {key}"; ctx.step(search, jql)`` consumes only
        ``jql``. Those are the same code and a model writes both — and they
        disagreed, so the same request passed or deadlocked depending on which
        spelling came back. A check flaky on formatting is one nobody can act
        on, and this asserts the equivalence rather than either shape, because
        pinning one shape is how they drifted apart.
        """
        inlined = workflow(
            '    picked = await ctx.agent("which epic?")\n'
            "    key = picked.text().strip()\n"
            '    rows = await ctx.step(search, f"parentEpic = {key}")\n'
            '    return f"# {key}\\n" + "\\n".join(r.summary for r in rows)\n'
        )
        bound = workflow(
            '    picked = await ctx.agent("which epic?")\n'
            "    key = picked.text().strip()\n"
            '    jql = f"parentEpic = {key} AND duedate < now()"\n'
            "    rows = await ctx.step(search, jql)\n"
            '    return f"# {key}\\n" + "\\n".join(r.summary for r in rows)\n'
        )

        assert not (await check(inlined)).issues
        assert not (await check(bound)).issues

    async def test_a_model_answer_no_durable_call_consumed_is_still_flagged(
        self,
    ) -> None:
        """The defect the stage was written for, unchanged.

        Nothing acted on this value — it went straight to the caller — so the
        de-tainting rule never applies and the finding survives.
        """
        code = workflow(
            "    rows = await ctx.step(fetch)\n"
            '    table = await ctx.agent("format these as markdown", rows)\n'
            "    return table\n"
        )

        assert (await check(code)).issues

    async def test_consuming_a_different_value_does_not_launder_the_answer(
        self,
    ) -> None:
        """De-tainting is per name, not per workflow.

        A body that both acts on one model decision and returns another is the
        defect in the half that is returned, and a rule keyed on the workflow
        rather than the value would miss it.
        """
        code = workflow(
            '    picked = await ctx.agent("which epic?")\n'
            "    key = picked.text()\n"
            "    rows = await ctx.step(search, key)\n"
            '    table = await ctx.agent("format these", rows)\n'
            "    return table\n"
        )

        assert (await check(code)).issues


class TestWhatItDoesNotSee:
    """The binding shapes the walk misses, asserted rather than assumed.

    All four are **false negatives** — a defect that goes unreported — and
    none can deadlock the repair loop, because a shape neither half of the
    analysis binds is a shape both halves ignore. That symmetry is the
    property worth keeping: taint and consumption read the same
    ``_assignment``, so a hole is a miss in both directions rather than a
    disagreement, and a disagreement is what makes a stage flag correct code
    it offers no way to fix.

    Closing them means tracking *values* rather than names. Two results of one
    model call, one of them acted on, are the same name-level subtree and
    different values — so a name-level fix that reported the second would have
    to keep the first, and reporting both is how the false positives come
    back. Left open deliberately, and pinned here so the gap is visible.
    """

    async def _clean(self, body: str) -> bool:
        return not (await check(workflow(body))).issues

    async def test_a_tuple_unpacked_model_value_is_missed(self) -> None:
        assert await self._clean(
            '    p = await ctx.agent("x")\n'
            '    head, tail = p.text().split("|")\n'
            "    return head\n"
        )

    async def test_a_loop_variable_over_model_output_is_missed(self) -> None:
        assert await self._clean(
            '    p = await ctx.agent("x")\n'
            "    out = []\n"
            "    for row in p.rows:\n"
            "        out.append(row)\n"
            '    return "".join(out)\n'
        )

    async def test_an_augmented_assignment_is_missed(self) -> None:
        assert await self._clean(
            '    p = await ctx.agent("x")\n'
            '    report = ""\n'
            "    report += p.text()\n"
            "    return report\n"
        )

    async def test_a_value_appended_into_a_list_is_missed(self) -> None:
        """A mutation is not a binding, and modelling it means modelling
        aliasing — a different analysis, not a wider one."""
        assert await self._clean(
            '    p = await ctx.agent("x")\n'
            "    out = []\n"
            "    out.append(p.text())\n"
            '    return "".join(out)\n'
        )


class TestWhereItSits:
    def test_it_is_non_blocking_and_static(self) -> None:
        """Nothing after it depends on the answer, and it costs an AST walk."""
        assert JudgementStage().blocking is False

        names = [stage.name for stage in default_stages()]
        assert names.index("judgement") < names.index("smoke")
