"""Scaffolding engine for template-fill code generation.

Provides workflow skeletons that constrain model output to known-good
patterns. Small models fill blanks rather than generating from scratch.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StepSkeleton(BaseModel):
    """Schema for a single step placeholder."""

    name: str
    description: str
    input_type: str = "dict"
    output_type: str = "dict"
    body_hint: str = ""


class WorkflowSkeleton(BaseModel):
    """Schema for a complete workflow template."""

    name: str
    description: str
    steps: list[StepSkeleton] = Field(default_factory=list)
    trigger: str = "manual"
    imports: list[str] = Field(default_factory=list)


class ScaffoldingEngine:
    """Generates fill-in-the-blank workflow code from templates.

    Templates are pre-registered workflow skeletons. The engine
    matches a user intent string against template names via simple
    keyword scoring and emits Python source with ``@step`` and
    ``@workflow`` decorators whose bodies contain
    ``raise NotImplementedError``.
    """

    def __init__(self) -> None:
        self._templates: dict[str, WorkflowSkeleton] = {}
        self._register_defaults()

    # --- public API ---------------------------------------------------

    def match_template(
        self,
        user_intent: str,
    ) -> WorkflowSkeleton | None:
        """Return the best-matching template or *None*.

        Scoring: each word in the template name that also appears
        in *user_intent* (case-insensitive) adds one point.
        """
        intent_lower = user_intent.lower()
        best: WorkflowSkeleton | None = None
        best_score = 0

        for name, skeleton in self._templates.items():
            keywords = name.split("_")
            score = sum(
                1 for kw in keywords if kw in intent_lower
            )
            if score > best_score:
                best_score = score
                best = skeleton

        return best

    def build_skeleton(
        self,
        intent: str,
        steps: list[dict[str, str]],
    ) -> str:
        """Generate a Python source scaffold.

        Each entry in *steps* must contain at least ``"name"``
        and ``"description"`` keys.  The generated code uses
        ``@step`` / ``@workflow`` decorators with
        ``raise NotImplementedError`` bodies.
        """
        lines: list[str] = [
            '"""Workflow: ' + intent + '."""',
            "",
            "from __future__ import annotations",
            "",
            "from loom import (",
            "    Context,",
            "    step,",
            "    workflow,",
            ")",
            "",
            "",
        ]

        step_names: list[str] = []
        for s in steps:
            name = s.get("name", "unnamed_step")
            desc = s.get("description", "TODO")
            step_names.append(name)
            lines.append(f'@step(name="{name}")')
            lines.append(f"async def {name}(ctx: Context) -> dict:")
            lines.append(f'    """{desc}."""')
            lines.append(
                "    raise NotImplementedError"
            )
            lines.append("")
            lines.append("")

        lines.append(f'@workflow(name="{intent}")')
        lines.append(
            "async def run(ctx: Context) -> dict:"
        )
        lines.append(f'    """{intent}."""')

        for sn in step_names:
            lines.append(
                f"    await ctx.step({sn})"
            )

        lines.append("")
        return "\n".join(lines)

    def list_templates(self) -> list[WorkflowSkeleton]:
        """Return all registered templates."""
        return list(self._templates.values())

    # --- internal -----------------------------------------------------

    def _register_defaults(self) -> None:
        """Populate the built-in template library."""
        self._templates["fetch_transform_notify"] = (
            WorkflowSkeleton(
                name="fetch_transform_notify",
                description=(
                    "Fetch data from a source, transform it,"
                    " and send a notification."
                ),
                steps=[
                    StepSkeleton(
                        name="fetch_data",
                        description="Fetch data from source",
                    ),
                    StepSkeleton(
                        name="transform_data",
                        description="Transform fetched data",
                    ),
                    StepSkeleton(
                        name="send_notification",
                        description="Send a notification",
                    ),
                ],
            )
        )

        self._templates["webhook_process_store"] = (
            WorkflowSkeleton(
                name="webhook_process_store",
                description=(
                    "Receive a webhook, process the payload,"
                    " and store results."
                ),
                trigger="webhook",
                steps=[
                    StepSkeleton(
                        name="validate_payload",
                        description=(
                            "Validate incoming payload"
                        ),
                    ),
                    StepSkeleton(
                        name="process_data",
                        description="Process validated data",
                    ),
                    StepSkeleton(
                        name="store_results",
                        description="Persist processed results",
                    ),
                ],
            )
        )

        self._templates["schedule_scrape_report"] = (
            WorkflowSkeleton(
                name="schedule_scrape_report",
                description=(
                    "Scrape a source on a schedule,"
                    " analyze the data, and distribute"
                    " a report."
                ),
                trigger="schedule",
                steps=[
                    StepSkeleton(
                        name="scrape_source",
                        description="Scrape data from source",
                    ),
                    StepSkeleton(
                        name="analyze_data",
                        description="Analyze scraped data",
                    ),
                    StepSkeleton(
                        name="generate_report",
                        description="Generate a report",
                    ),
                    StepSkeleton(
                        name="distribute_report",
                        description="Distribute the report",
                    ),
                ],
            )
        )

        self._templates["ai_pipeline"] = WorkflowSkeleton(
            name="ai_pipeline",
            description=(
                "Prepare input for an LLM, call it,"
                " validate the output, and post-process."
            ),
            steps=[
                StepSkeleton(
                    name="prepare_input",
                    description="Prepare input for the LLM",
                ),
                StepSkeleton(
                    name="call_llm",
                    description="Call the language model",
                ),
                StepSkeleton(
                    name="validate_output",
                    description="Validate LLM output",
                ),
                StepSkeleton(
                    name="post_process",
                    description="Post-process validated output",
                ),
            ],
        )
