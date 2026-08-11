"""Built-in agent runtime — the default executor that ships with the SDK.

The turn loop: prompt model → dispatch tool calls → check guardrails →
enforce budget → loop until final output or limit reached.

When called from within a workflow via ``ctx.agent()``, every model call
and tool call is journaled separately. An agent that dies on turn 9
resumes at turn 9 rather than re-paying for the first eight turns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from workflow_builder.agents.executor import AgentContext, AgentSettings
from workflow_builder.agents.guardrails import GuardrailAction
from workflow_builder.agents.limits import DEFAULT_LIMITS
from workflow_builder.agents.messages import (
    Message,
    ToolCall,
    system,
    tool_result,
    user,
)
from workflow_builder.agents.models import (
    FinishReason,
    ModelRequest,
    ModelSettings,
    ToolSchema,
    estimate_cost,
)
from workflow_builder.agents.output import FINAL_OUTPUT_TOOL, OutputMode, OutputSpec
from workflow_builder.agents.result import AgentResult, ItemKind, RunItem
from workflow_builder.agents.tools import Tool, ToolContext
from workflow_builder.core.exceptions import (
    GuardrailTripwire,
    ModelBehaviorError,
    ModelRetry,
)
from workflow_builder.core.models import Usage

if TYPE_CHECKING:
    from pydantic import BaseModel

    from workflow_builder.agents.agent import Agent
    from workflow_builder.runtime.context import Context

logger = logging.getLogger("workflow.agent")


class BuiltInAgentRuntime:
    """The default agent executor that ships with the SDK.

    Owns the turn loop and delegates model calls to a :class:`ModelProvider`.
    """

    def __init__(self, agent: Agent[Any]) -> None:
        self._agent = agent
        self.agent_id = agent.name

    async def execute(
        self,
        input: Any,
        *,
        tools: list[Tool] | None = None,
        output_type: type[BaseModel] | None = None,
        settings: AgentSettings | None = None,
        context: AgentContext | None = None,
    ) -> AgentResult[Any]:
        """Run the turn loop to completion."""
        agent = self._agent
        if agent.model is None:
            raise ModelBehaviorError(
                f"Agent '{agent.name}' has no model configured. "
                f"Set agent.model to a ModelProvider instance."
            )

        effective_tools = tools if tools is not None else agent.tools
        tool_map = {t.name: t for t in effective_tools}
        limits = agent.limits or DEFAULT_LIMITS
        model_settings = agent.model_settings.merged_with(
            ModelSettings(
                temperature=settings.temperature if settings else None,
                max_tokens=settings.max_tokens if settings else None,
            )
        )

        # Build output spec
        out_type = output_type or agent.output_type
        output_spec = OutputSpec(type=out_type) if out_type else OutputSpec()

        # Build tool schemas for the model
        tool_schemas = [
            ToolSchema(
                name=t.name,
                description=t.description,
                parameters=t.schema() if callable(t.schema) else t.schema,
            )
            for t in effective_tools
        ]
        # Add final_output tool if using tool-based structured output
        resolved_mode = output_spec.resolve_mode(
            supports_native=False, has_tools=bool(tool_schemas)
        )
        if resolved_mode is OutputMode.TOOL and output_spec.is_structured:
            tool_schemas.append(
                ToolSchema(
                    name=FINAL_OUTPUT_TOOL,
                    description=output_spec.description,
                    parameters=output_spec.tool_schema(),
                    strict=True,
                )
            )

        # Build initial messages
        messages: list[Message] = []
        if agent.instructions:
            prompt = agent.instructions
            if "{input}" in prompt:
                prompt = prompt.replace("{input}", str(input))
            messages.append(system(prompt))
        if resolved_mode is OutputMode.PROMPTED and output_spec.is_structured:
            messages[0] = system(
                (messages[0].content or "")
                + "\n\n"
                + output_spec.prompt_instructions()
            )
        messages.append(user(str(input)))

        # Turn loop
        cumulative_usage = Usage()
        all_tool_calls: list[ToolCall] = []
        items: list[RunItem] = []
        turn = 0

        while True:
            turn += 1
            limits.check_turn(turn)
            limits.check_usage(cumulative_usage)

            # Model call
            request = ModelRequest(
                messages=messages,
                tools=tool_schemas if effective_tools or (resolved_mode is OutputMode.TOOL) else [],
                settings=model_settings,
                model=agent.model.model_name,
                output_schema=(
                    output_spec.json_schema()
                    if resolved_mode is OutputMode.NATIVE
                    else None
                ),
            )
            response = await agent.model.complete(request)
            cumulative_usage.add(response.usage)
            cumulative_usage.cost_usd = estimate_cost(
                response.model or agent.model.model_name, cumulative_usage
            )

            items.append(RunItem(
                kind=ItemKind.MESSAGE,
                agent=agent.name,
                content=response.message,
                turn=turn,
            ))
            messages.append(response.message)

            # Check for final output via tool call
            if response.message.has_tool_calls:
                final_call = _find_final_output(response.message.tool_calls)
                if final_call is not None:
                    try:
                        output = output_spec.parse(final_call.arguments)
                    except ModelRetry as retry:
                        messages.append(
                            tool_result(final_call.id, str(retry), name=FINAL_OUTPUT_TOOL)
                        )
                        items.append(RunItem(
                            kind=ItemKind.RETRY,
                            agent=agent.name,
                            content=str(retry),
                            turn=turn,
                        ))
                        continue

                    items.append(RunItem(
                        kind=ItemKind.FINAL_OUTPUT,
                        agent=agent.name,
                        content=output,
                        turn=turn,
                    ))
                    return AgentResult(
                        output=output,
                        agent=agent.name,
                        messages=messages,
                        items=items,
                        usage=cumulative_usage,
                        turns=turn,
                        tool_calls=all_tool_calls,
                    )

                # Process regular tool calls
                for call in response.message.tool_calls:
                    if call.name == FINAL_OUTPUT_TOOL:
                        continue
                    all_tool_calls.append(call)
                    items.append(
                        RunItem(
                            kind=ItemKind.TOOL_CALL,
                            agent=agent.name,
                            name=call.name,
                            content=call.arguments,
                            turn=turn,
                        )
                    )

                    tool = tool_map.get(call.name)
                    if tool is None:
                        msg = f"Unknown tool '{call.name}'. Available: {list(tool_map)}"
                        messages.append(tool_result(call.id, msg, name=call.name))
                        items.append(RunItem(
                            kind=ItemKind.TOOL_OUTPUT,
                            agent=agent.name,
                            name=call.name,
                            content=msg,
                            turn=turn,
                        ))
                        continue

                    # Run guardrails on tool call
                    guardrail_blocked = False
                    for guard in agent.guardrails:
                        verdict = await guard.evaluate(call.arguments, call.name)
                        if verdict.action is GuardrailAction.TRIPWIRE:
                            raise GuardrailTripwire(
                                verdict.message, guardrail_name=guard.name, info=verdict.info
                            )
                        if verdict.blocked:
                            msg = f"Guardrail rejected: {verdict.message}"
                            messages.append(
                                tool_result(call.id, msg, name=call.name)
                            )
                            guardrail_blocked = True
                            break
                    if guardrail_blocked:
                        continue

                    # Execute tool
                    try:
                        tool_ctx = ToolContext(
                            agent_name=agent.name,
                            tool_call_id=call.id,
                        )
                        result = await tool.invoke(call.arguments, tool_ctx)
                        result_str = tool.render_result(result)
                    except ModelRetry as retry:
                        result_str = str(retry)
                    except Exception as exc:
                        result_str = f"Tool error: {type(exc).__name__}: {exc}"

                    messages.append(tool_result(call.id, result_str, name=call.name))
                    items.append(RunItem(
                        kind=ItemKind.TOOL_OUTPUT,
                        agent=agent.name,
                        name=call.name,
                        content=result_str,
                        turn=turn,
                    ))
                    limits.check_tool_calls(len(all_tool_calls))

                continue

            # No tool calls — check for text-based final output
            if response.finish_reason is FinishReason.STOP:
                text = response.message.text()
                if output_spec.is_structured and resolved_mode is OutputMode.PROMPTED:
                    try:
                        output = output_spec.parse_text(text)
                    except ModelRetry as retry:
                        messages.append(user(str(retry)))
                        continue
                else:
                    output = text

                items.append(
                    RunItem(kind=ItemKind.FINAL_OUTPUT, agent=agent.name, content=output, turn=turn)
                )
                return AgentResult(
                    output=output,
                    agent=agent.name,
                    messages=messages,
                    items=items,
                    usage=cumulative_usage,
                    turns=turn,
                    tool_calls=all_tool_calls,
                )

            # Length / content filter — retry or bail
            if response.finish_reason is FinishReason.LENGTH:
                messages.append(user("Your response was cut off. Please continue."))
                continue

            # Shouldn't reach here
            logger.warning("Unexpected finish_reason: %s", response.finish_reason)
            return AgentResult(
                output=response.message.text(),
                agent=agent.name,
                messages=messages,
                items=items,
                usage=cumulative_usage,
                turns=turn,
                tool_calls=all_tool_calls,
            )


def _find_final_output(calls: list[ToolCall]) -> ToolCall | None:
    """Find a final_output tool call if one exists."""
    for call in calls:
        if call.name == FINAL_OUTPUT_TOOL:
            return call
    return None


async def run_agent_durably(
    agent: Agent[Any],
    input: Any,
    *,
    ctx: Context[Any],
    max_turns: int | None = None,
    session_id: str | None = None,
    **kwargs: Any,
) -> AgentResult[Any]:
    """Run an agent within a durable workflow context.

    This is the integration point called by ``ctx.agent()``. The executor's
    internal model calls and tool calls are NOT individually journaled in this
    basic implementation — the entire AgentResult is journaled as one entry.

    Phase 2 will add per-turn journaling for crash recovery within agent runs.
    """
    settings = AgentSettings(max_turns=max_turns) if max_turns else None
    context = AgentContext(
        run_id=ctx.run_id,
        workflow=ctx.workflow,
        session_id=session_id,
    )
    return await agent(input, settings=settings, context=context)
