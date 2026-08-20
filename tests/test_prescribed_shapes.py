"""Code the prompt prescribes must not be an error.

Twice now a stage has flagged the very shape another part of the system tells
a model to write, and both times the cost was the same: ``report.errors``
drives repair, so the loop rewrote correct code into another spelling of
itself until the round budget ran out. ``ResolutionStage`` errored on the
scoped JQL search that is the only way to look up a Jira epic;
``JudgementStage`` then errored on the ``ctx.agent()`` disambiguation that
``ResolutionStage``'s own message asks for.

Each was found by running the example and reading eight minutes of output.
This is the same finding as a gate: every shape below is one the system
*instructs*, so a stage reporting an error on it is wrong by construction and
no judgement call is needed to say so.

**Errors only, deliberately.** A warning is advice a model can decline —
unchanged code ends the repair — so warnings are allowed here and are often
the right answer. What is forbidden is the state with no exit.

The stages run without smoke, lint or types: those need a subprocess and
answer questions about the environment rather than about the shape.
"""

from __future__ import annotations

import pytest

from loom.agents.checks import CheckContext
from loom.agents.stages import (
    CataloguePreferenceStage,
    CoverageStage,
    IdentifierStage,
    JudgementStage,
    PlacementStage,
    ProjectionStage,
    ResolutionStage,
)


@pytest.fixture(scope="module")
def registry():
    from loom.agents.tool_registry import ToolsetRegistry
    from loom.toolsets.jira.manifest import JIRA_MANIFEST

    found = ToolsetRegistry()
    found.register(JIRA_MANIFEST)
    return found


def _stages(registry):
    """The stages that read a shape rather than an environment."""
    return [
        CoverageStage(registry),
        PlacementStage(),
        ResolutionStage(registry),
        ProjectionStage(),
        CataloguePreferenceStage(),
        IdentifierStage(registry),
        JudgementStage(),
    ]


HEAD = (
    "from loom import Context, workflow\n"
    "from loom.toolsets.jira.tools import (\n"
    "    jira_resolve_epic,\n"
    "    jira_search_issues,\n"
    ")\n"
    "\n"
    "\n"
    '@workflow(name="overdue")\n'
    "async def overdue(ctx: Context, data: dict) -> str:\n"
)

SPEC = "list every ticket past its due date in the saas epic"

#: Each entry is a rung of the resolution ladder, or a rule the system prompt
#: states outright, written the way the instructions describe it.
PRESCRIBED: dict[str, str] = {
    # Rung 2 — resolve while authoring, bake the id in with the name beside it.
    "a resolved id baked in with the human name in a comment": (
        '    epic = "PA-1844"  # "SaaS V2", resolved at authoring time\n'
        "    rows = await ctx.step(\n"
        "        jira_search_issues,\n"
        '        f"parentEpic = {epic} AND duedate < now()",\n'
        "        200,\n"
        "    )\n"
        '    return "\\n".join(f"{r.key} {r.summary}" for r in rows)\n'
    ),
    # Rung 3 — ambiguous, so a model chooses between candidates the resolver
    # returned, and the choice is then used to fetch.
    "an ambiguity handed to ctx.agent, inlined into the query": (
        '    found = await ctx.step(jira_resolve_epic, "saas")\n'
        "    if len(found.matches) == 1:\n"
        "        epic = found.matches[0].key\n"
        "    else:\n"
        '        picked = await ctx.agent(f"which epic? {found.note}")\n'
        "        epic = picked.text().strip()\n"
        "    rows = await ctx.step(\n"
        "        jira_search_issues,\n"
        '        f"parentEpic = {epic} AND duedate < now()",\n'
        "        200,\n"
        "    )\n"
        '    header = f"# Overdue in {epic}"\n'
        '    return header + "\\n" + "\\n".join(r.key for r in rows)\n'
    ),
    # The same code with the query bound to a name first. A model writes both
    # and they must read the same; this pair is why consumption follows
    # bindings rather than reading a call's arguments only.
    "an ambiguity handed to ctx.agent, query bound to a name": (
        '    found = await ctx.step(jira_resolve_epic, "saas")\n'
        "    if len(found.matches) == 1:\n"
        "        epic = found.matches[0].key\n"
        "    else:\n"
        '        picked = await ctx.agent(f"which epic? {found.note}")\n'
        "        epic = picked.text().strip()\n"
        '    jql = f"parentEpic = {epic} AND duedate < now()"\n'
        "    rows = await ctx.step(jira_search_issues, jql, 200)\n"
        '    header = f"# Overdue in {epic}"\n'
        '    return header + "\\n" + "\\n".join(r.key for r in rows)\n'
    ),
    # Rung 4 — every namespace searched, nothing bears the name, so the text
    # match stays and the return says so. The message promises this is
    # accepted; a stage erroring on it would make the promise false.
    # The prompt's coverage rule: report what a paged read did not cover
    # instead of presenting one page as the whole answer.
    "a paged read whose coverage is reported": (
        '    rows = await ctx.step(jira_search_issues, "duedate < now()", 500)\n'
        "    note = \"\" if rows.complete else f\"_{rows.summary()}_\"\n"
        '    return note + "\\n".join(r.key for r in rows)\n'
    ),
}


@pytest.mark.parametrize("shape", sorted(PRESCRIBED), ids=lambda s: s[:40])
async def test_a_prescribed_shape_is_never_an_error(shape, registry) -> None:
    code = HEAD + PRESCRIBED[shape]
    context = CheckContext(spec=SPEC)

    errors: list[str] = []
    for stage in _stages(registry):
        result = await stage.run(code, context)
        errors += [
            f"{stage.name}: {issue.message}"
            for issue in result.issues
            if issue.severity == "error"
        ]

    assert not errors, "\n  ".join(["a prescribed shape errored:", *errors])


async def test_the_corpus_is_real_python(registry) -> None:
    """A fixture that does not parse finds nothing and passes for the wrong
    reason — the failure mode this file's own subject matter keeps producing.
    """
    import ast

    for shape, body in PRESCRIBED.items():
        ast.parse(HEAD + body), shape


async def test_the_last_rung_errors_by_design_and_settles_on_a_decline() -> None:
    """Rung 4 is the one prescribed shape that *does* error, deliberately.

    "You have searched every namespace and nothing bears that name" is not
    something the check can verify — only the model knows which lookups it
    ran — so the finding is raised and the model answers it by leaving the
    file alone. ``_settle_advisories`` then downgrades it, which is what keeps
    ``is_clean`` true and lets the code run.

    The cost is real and is the reason this sits outside the corpus above: it
    spends a repair round, and it works only because the model *declines*
    rather than trying again. A model that keeps editing never reaches the
    settle, which is exactly how the resolution deadlock burned three rounds.
    """
    from loom.agents.coding_agent import _settle_advisories
    from loom.agents.tool_registry import ToolsetRegistry
    from loom.toolsets.jira.manifest import JIRA_MANIFEST

    found = ToolsetRegistry()
    found.register(JIRA_MANIFEST)
    code = HEAD + (
        "    rows = await ctx.step(\n"
        "        jira_search_issues,\n"
        '        \'text ~ "saas" AND duedate < now()\',\n'
        "        200,\n"
        "    )\n"
        '    return "nothing bears the name saas, so it was matched as text"\n'
    )

    result = await ResolutionStage(found).run(code, CheckContext(spec=SPEC))

    assert [i.severity for i in result.issues] == ["error"]
    settled = _settle_advisories(list(result.issues), declined=True)
    assert [i.severity for i in settled] == ["warning"]


async def test_the_gate_can_fail(registry) -> None:
    """A shape the system does *not* prescribe still errors, so a green run
    above means the stages ran rather than that they were toothless."""
    guess = HEAD + (
        "    rows = await ctx.step(\n"
        "        jira_search_issues,\n"
        '        \'summary ~ "saas"\',\n'
        "        200,\n"
        "    )\n"
        '    return "\\n".join(r.key for r in rows)\n'
    )

    result = await ResolutionStage(registry).run(guess, CheckContext(spec=SPEC))

    assert [i.severity for i in result.issues] == ["error"]
