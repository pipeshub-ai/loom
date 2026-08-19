"""Supervisor review — a second opinion on generated workflow code.

The coding agent grades its own homework: it writes the code, runs its own
validator, and decides when it is done. That catches malformed code and code
that fails to run, but not code that runs perfectly and does the wrong thing —
retries a non-idempotent charge, does I/O in the workflow body, swallows an
error that should surface.

A supervisor is a separate model call with no stake in the original answer,
given the spec and the code and asked what is wrong with it. It reviews rather
than rewrites: its output is findings, and the author agent does the fixing,
because a reviewer that edits tends to introduce its own bugs while removing
someone else's.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("workflow.supervisor")


DEFAULT_SUPERVISOR_PROMPT = textwrap.dedent("""\
    You are reviewing a workflow written for the LOOM durable-execution SDK by
    another engineer. You did not write it and have no stake in it.

    Judge only what would actually go wrong when this runs. Ignore style,
    naming, and formatting entirely.

    Look for:

    - **Durability**: I/O, network calls, or file access in the workflow body
      instead of inside a @step. These re-execute on every replay.
    - **Determinism**: datetime.now(), uuid4(), random, or reading mutable
      global state in the workflow body. Must use ctx.now() / ctx.uuid4() /
      ctx.random().
    - **Retry safety**: a step that is retried but is not idempotent — charging
      a card, sending an email, appending to a ledger. Either it needs an
      idempotency key or it should not be retried.
    - **Error handling**: failures swallowed silently, or a bare except that
      hides a real problem. Note that catching Exception cannot swallow LOOM's
      control signals, so suspension is not a concern.
    - **Spec fidelity**: something the specification asked for that the code
      does not do, or does differently.

    Be specific. "This could be better" is not a finding; "charge_card is
    retried three times without an idempotency key, so a timeout on the first
    attempt double-charges" is.

    Approve when nothing above applies. A clean review is a valid outcome and
    inventing a finding to look thorough makes the review worthless.
""")


class Finding(BaseModel):
    """One problem the supervisor identified."""

    severity: str = Field(description="'error' if it will misbehave, else 'warning'")
    category: str = Field(
        description="One of: durability, determinism, retry-safety, "
        "error-handling, spec-fidelity"
    )
    message: str = Field(description="What is wrong and what goes wrong because of it")


class SupervisorVerdict(BaseModel):
    """The outcome of one review."""

    approved: bool = Field(description="True when the code is fit to hand over")
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def blocking(self) -> bool:
        """Whether this verdict should send the code back for repair."""
        return not self.approved or bool(self.errors)

    def as_feedback(self, code: str = "") -> str:
        """Phrase the findings as a revision request for the author agent.

        Pass the *code*: the author agent is ephemeral, so a revision round that
        sends only the findings asks it to edit something it can no longer see.
        """
        if not self.findings:
            return "A reviewer rejected the code but listed no findings. Re-check it."

        lines = [
            "A reviewer found problems with the workflow you generated. Fix each "
            "one and return the complete corrected file.",
            "",
            "## Findings",
            "",
        ]
        lines += [f"- [{f.severity}/{f.category}] {f.message}" for f in self.findings]
        if code:
            lines += ["", "## The code under review", "", f"```python\n{code}\n```"]
        return "\n".join(lines)


class CodeSupervisor:
    """Reviews generated code with a model that did not write it.

    Parameters
    ----------
    model:
        A ``ModelProvider``. Use a different one from the author where you can —
        two models rarely share a blind spot, and one model reviewing itself
        mostly agrees with itself.
    instructions:
        Replaces :data:`DEFAULT_SUPERVISOR_PROMPT` outright.
    extra_instructions:
        Appended — house rules, domain constraints, things your team has been
        bitten by before.
    """

    def __init__(
        self,
        model: Any,
        *,
        instructions: str | None = None,
        extra_instructions: str = "",
    ) -> None:
        self._model = model
        self._instructions = instructions
        self._extra_instructions = extra_instructions

    def build_prompt(self) -> str:
        """The full system prompt, exposed so callers can inspect it."""
        parts = [
            self._instructions
            if self._instructions is not None
            else DEFAULT_SUPERVISOR_PROMPT
        ]
        if self._extra_instructions:
            parts.append(self._extra_instructions)
        return "\n\n".join(parts)

    async def review(self, spec: str, code: str) -> SupervisorVerdict:
        """Review *code* against *spec*.

        Never raises: a supervisor that errors must not fail the generation it
        was only advising on. An unusable review approves by default and says so
        in the summary, so the failure is visible without being fatal.
        """
        from loom.agents.agent import Agent
        from loom.agents.limits import UsageLimits
        from loom.agents.models import ModelSettings

        reviewer: Agent[Any] = Agent(
            name="workflow_supervisor",
            instructions=self.build_prompt(),
            model=self._model,
            output_type=SupervisorVerdict,
            # Low temperature: a review should be reproducible.
            model_settings=ModelSettings(temperature=0.0),
            limits=UsageLimits(max_turns=2),
        )

        request = f"## Specification\n\n{spec}\n\n## Code under review\n\n```python\n{code}\n```"
        try:
            result = await reviewer(request)
        except Exception:
            logger.exception("supervisor review failed; approving by default")
            return SupervisorVerdict(
                approved=True, summary="review unavailable: the supervisor errored"
            )

        verdict = result.output
        if isinstance(verdict, dict):
            verdict = SupervisorVerdict.model_validate(verdict)
        if not isinstance(verdict, SupervisorVerdict):
            return SupervisorVerdict(
                approved=True,
                summary="review unavailable: the supervisor returned no verdict",
            )

        logger.info(
            "supervisor | approved=%s findings=%d",
            verdict.approved,
            len(verdict.findings),
        )
        return verdict
