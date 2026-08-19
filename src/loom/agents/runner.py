"""Built-in agent runtime — the default executor that ships with the SDK.

The turn loop: prompt model → dispatch tool calls → check guardrails →
enforce budget → loop until final output or limit reached.

When called from within a workflow via ``ctx.agent()``, every model call
and tool call is journaled separately. An agent that dies on turn 9
resumes at turn 9 rather than re-paying for the first eight turns.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from loom.agents.bounds import bound_result
from loom.agents.executor import AgentContext, AgentSettings
from loom.agents.guardrails import GuardrailAction
from loom.agents.limits import DEFAULT_LIMITS
from loom.agents.memory import trim_history
from loom.agents.messages import (
    Message,
    ToolCall,
    system,
    tool_result,
    user,
)
from loom.agents.models import (
    FinishReason,
    ModelRequest,
    ModelSettings,
    ToolSchema,
    estimate_cost,
)
from loom.agents.output import FINAL_OUTPUT_TOOL, OutputMode, OutputSpec
from loom.agents.result import AgentResult, ItemKind, RunItem
from loom.agents.tools import Tool, ToolContext
from loom.core.exceptions import (
    AgentStopped,
    GuardrailTripwire,
    ModelBehaviorError,
    ModelRetry,
)
from loom.core.models import Usage
from loom.runtime.effects import EffectCall, EffectDenied

if TYPE_CHECKING:
    from pydantic import BaseModel

    from loom.agents.agent import Agent
    from loom.runtime.context import Context

logger = logging.getLogger("workflow.agent")

#: Separate from the agent logger so tool traffic can be turned on without the
#: rest, which is the thing anyone debugging a loop actually wants.
tool_logger = logging.getLogger("workflow.agent.tools")


def _brief(value: Any, limit: int = 120) -> str:
    """Render a value for a log line: one line, bounded length."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


class BuiltInAgentRuntime:
    """The default agent executor that ships with the SDK.

    Owns the turn loop and delegates model calls to a :class:`ModelProvider`.
    """

    def __init__(self, agent: Agent[Any]) -> None:
        self._agent = agent
        self.agent_id = agent.name

    @staticmethod
    def _spill_store(context: Any) -> Any:
        """Where an oversized tool result is stored, if anywhere.

        Derived from the Runtime's blob service rather than configured
        separately: a deployment that has somewhere to put large values already
        said so once, and asking twice is how the two drift apart.
        """
        return getattr(context, "spill", None)

    async def execute(
        self,
        input: Any,
        *,
        tools: list[Tool] | None = None,
        output_type: type[BaseModel] | None = None,
        settings: AgentSettings | None = None,
        context: AgentContext | None = None,
    ) -> AgentResult[Any]:
        """Run the turn loop, with agent-family hooks around it.

        Wrapping here rather than at each of the loop's three returns means
        "the agent finished" is one place and every exit is covered by
        construction. The whole run is a single journal entry, so none of this
        is reached on a replay.
        """
        hooks = getattr(context, "hooks", None)
        if hooks is None or not hooks.has_agent:
            return await self._execute_inner(
                input,
                tools=tools,
                output_type=output_type,
                settings=settings,
                context=context,
            )

        from loom.runtime.hooks import AgentHookContext

        run_id = getattr(context, "run_id", "")
        opening = AgentHookContext(
            agent_name=self._agent.name, run_id=run_id, input=input
        )
        await hooks.dispatch_agent("agent_start", opening)

        turns = _Turns(hooks, self._agent.name, run_id)
        try:
            result = await self._execute_inner(
                input,
                tools=tools,
                output_type=output_type,
                settings=settings,
                context=context,
                turns=turns,
            )
        except BaseException as exc:
            await turns.close()
            await hooks.dispatch_agent(
                "agent_end",
                AgentHookContext(
                    agent_name=self._agent.name, run_id=run_id, input=input, error=exc
                ),
            )
            raise
        await turns.close()
        await hooks.dispatch_agent(
            "agent_end",
            AgentHookContext(
                agent_name=self._agent.name,
                run_id=run_id,
                input=input,
                result=result,
                turn=result.turns,
            ),
        )
        return result

    async def _execute_inner(
        self,
        input: Any,
        *,
        tools: list[Tool] | None = None,
        output_type: type[BaseModel] | None = None,
        settings: AgentSettings | None = None,
        context: AgentContext | None = None,
        turns: _Turns | None = None,
    ) -> AgentResult[Any]:
        """Run the turn loop to completion."""
        agent = self._agent
        if agent.model is None:
            raise ModelBehaviorError(
                f"Agent '{agent.name}' has no model configured. "
                f"Set agent.model to a ModelProvider instance."
            )

        effective_tools = list(tools if tools is not None else agent.tools)
        if getattr(agent, "user_interaction", None) is not None and not any(
            t.name == "ask_user" for t in effective_tools
        ):
            from loom.agents.interaction import make_ask_user_tool

            effective_tools.append(make_ask_user_tool(agent.user_interaction))

        # Mounted up front, not when a result first overflows: the model sees
        # the tool list before the call that overflows, and a locator it has no
        # way to follow is just a more informative truncation.
        store = self._spill_store(context)
        if agent.bounds is not None and store is not None:
            from loom.agents.spill_tools import spill_tools

            known = {t.name for t in effective_tools}
            effective_tools.extend(
                t for t in spill_tools(store) if t.name not in known
            )
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
        if context is not None and context.history:
            # Prior turns sit between the system prompt and this turn's input, so
            # the agent sees the conversation in the order it happened.
            messages.extend(
                trim_history(context.history, max_messages=limits.max_history_messages)
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
            if turns is not None:
                await turns.begin(turn, messages)
                turns.check(turn)

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
            if turns is not None:
                # `messages` is mutated in place by the hook, so the request is
                # rebuilt from it rather than reusing the one composed above.
                await turns.before_model(turn, messages)
                request = request.model_copy(update={"messages": messages})
            response = await agent.model.complete(request)
            if turns is not None:
                await turns.after_model(turn, messages, response)
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
                    started = time.monotonic()
                    outcome = "ok"
                    result: Any = None
                    try:
                        tool_ctx = ToolContext(
                            run_id=context.run_id if context else "",
                            agent_name=agent.name,
                            tool_call_id=call.id,
                            deps=getattr(context, "deps", None) if context else None,
                            workflow_ctx=(
                                getattr(context, "workflow_ctx", None) if context else None
                            ),
                        )
                        result = await _dispatch_tool(tool, call, tool_ctx, context)
                        result_str = tool.render_result(result)
                    except EffectDenied as denied:
                        # Answered rather than raised: an agent told what it may
                        # not do can pick another route, where a raised denial
                        # ends the run and loses the turn's work. The refusal
                        # names the grant, so the message is actionable to
                        # whoever reads the transcript too.
                        result_str = str(denied)
                        outcome = "denied"
                    except ModelRetry as retry:
                        result_str = str(retry)
                        outcome = "retry"
                    except Exception as exc:
                        result_str = f"Tool error: {type(exc).__name__}: {exc}"
                        outcome = "error"

                    # One line per call, at DEBUG: what was asked, what came
                    # back, and how long it took. An agent that loops does so
                    # invisibly otherwise — the turn count says it happened,
                    # not what it was doing.
                    tool_logger.debug(
                        "turn=%d %s(%s) -> %s %s in %.0fms",
                        turn,
                        call.name,
                        _brief(call.arguments),
                        outcome,
                        _brief(result_str, limit=160),
                        (time.monotonic() - started) * 1000,
                    )

                    # Bound what the *conversation* carries. The item below
                    # keeps the rendered result as-is, and the journal keeps
                    # the value whole: a replay has to reconstruct the run that
                    # happened, and an unbounded result is still what the tool
                    # returned. Only the model's view is capped.
                    bounded = await bound_result(
                        result_str,
                        result if outcome == "ok" else None,
                        bounds=agent.bounds,
                        store=self._spill_store(context),
                        run_id=context.run_id if context else "",
                        tool=call.name,
                        call_id=call.id,
                    )
                    messages.append(tool_result(call.id, bounded, name=call.name))
                    items.append(RunItem(
                        kind=ItemKind.TOOL_OUTPUT,
                        agent=agent.name,
                        name=call.name,
                        # The *bounded* text once bounds are configured, not the
                        # raw result. `messages` and `items` both live inside the
                        # one AgentResult that becomes one journal entry, so
                        # keeping the full text here meant `ResultBounds` capped
                        # the model's view and nothing else: twenty 4 MB tool
                        # calls still concentrated ~80 MB into a single row.
                        # Without bounds this is unchanged — the journal keeps
                        # the value whole, because a replay has to reconstruct
                        # the run that happened.
                        content=bounded if agent.bounds is not None else result_str,
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


class _Turns:
    """Fires ``turn_start``/``turn_end`` exactly once per turn.

    A ``try/finally`` around the loop body would be the obvious implementation
    and would mean re-indenting two hundred lines of working turn loop. This
    closes the previous turn when the next one opens, and the caller closes the
    last one on the way out — so every exit from the loop, including the three
    ``return``s and any raise, still produces exactly one ``turn_end`` per
    ``turn_start``.
    """

    __slots__ = (
        "_agent_name",
        "_hooks",
        "_messages",
        "_open",
        "_run_id",
        "stop_reason",
        "stopped",
    )

    def __init__(self, hooks: Any, agent_name: str, run_id: str) -> None:
        self._hooks = hooks
        self._agent_name = agent_name
        self._run_id = run_id
        self._open = 0
        self._messages: list[Any] = []
        self.stopped = False
        self.stop_reason = ""

    def _ctx(self, turn: int, **kw: Any) -> Any:
        from loom.runtime.hooks import AgentHookContext

        return AgentHookContext(
            agent_name=self._agent_name, run_id=self._run_id, turn=turn, **kw
        )

    async def begin(self, turn: int, messages: list[Any]) -> None:
        await self.close()
        self._open = turn
        self._messages = messages
        await self._hooks.dispatch_agent(
            "turn_start", self._ctx(turn, messages=messages)
        )

    async def close(self, response: Any = None) -> None:
        if not self._open:
            return
        turn, self._open = self._open, 0
        ctx = self._ctx(turn, messages=self._messages, response=response)
        await self._hooks.dispatch_agent("turn_end", ctx)
        self._note_stop(ctx)

    def _note_stop(self, ctx: Any) -> None:
        """Remember a hook's request to end the loop.

        Recorded rather than acted on here, because a hook asking to stop is
        asking for the loop to end after this turn — not for the turn it is
        observing to be abandoned halfway through.
        """
        if getattr(ctx, "stopped", False) and not self.stopped:
            self.stopped = True
            self.stop_reason = ctx.stop_reason

    def check(self, turn: int) -> None:
        """Raise if a hook asked to stop. Called at the top of the next turn."""
        if self.stopped:
            raise AgentStopped(
                f"agent '{self._agent_name}' was stopped by a hook after turn "
                f"{turn - 1}" + (f": {self.stop_reason}" if self.stop_reason else ""),
                turn=turn - 1,
                reason=self.stop_reason,
            )

    async def before_model(self, turn: int, messages: list[Any]) -> None:
        """Where message shaping happens — compaction, trimming, redaction.

        The list is mutated in place by the hook and the request rebuilt from
        it, so a middleware that reassigns rather than mutates is a no-op. That
        is the one sharp edge of this event and it is documented on
        ``AgentHookContext.messages``.
        """
        await self._hooks.dispatch_agent(
            "model_start", self._ctx(turn, messages=messages)
        )

    async def after_model(self, turn: int, messages: list[Any], response: Any) -> None:
        ctx = self._ctx(turn, messages=messages, response=response)
        await self._hooks.dispatch_agent("model_end", ctx)
        self._note_stop(ctx)


async def _dispatch_tool(
    tool: Tool,
    call: ToolCall,
    tool_ctx: ToolContext,
    context: AgentContext | None,
) -> Any:
    """Invoke a tool, through the broker when one is in play.

    Outside a workflow there is no broker and no authority, and the call runs
    exactly as it always has — an agent used directly is not a durable run, and
    inventing a policy for it would be inventing one nobody set.

    ``needs_approval`` is the exception, and applies with or without a broker:
    it is not an invented policy but one the tool's author wrote on the tool,
    and a declaration that silently does nothing is worse than no declaration —
    somebody sets ``needs_approval=lambda args: args["amount_cents"] > 50_00``
    and believes the refund is gated.
    """
    tool.enforce_approval(dict(call.arguments or {}))

    broker = getattr(context, "broker", None)
    if broker is None:
        return await tool.invoke(call.arguments, tool_ctx)

    from loom.security.authority import Authority
    from loom.toolsets.manifest import EffectClass

    metadata = tool.metadata or {}
    toolset = metadata.get("toolset", "")
    operation = metadata.get("operation", tool.name)

    async def perform() -> Any:
        return await tool.invoke(call.arguments, tool_ctx)

    outcome = await broker.dispatch(
        EffectCall(
            kind="tool",
            target=f"{toolset}.{operation}" if toolset else operation,
            arguments=dict(call.arguments or {}),
            effect=metadata.get("effect") or EffectClass.WRITE,
            open_world=metadata.get("open_world", True),
            reversible=metadata.get("reversible", False),
            access_control=metadata.get("access_control", False),
            run_id=getattr(context, "run_id", ""),
            perform=perform,
        ),
        getattr(context, "authority", None) or Authority(),
    )
    if outcome.ok:
        return outcome.value
    raise EffectDenied(
        outcome.error or f"tool '{tool.name}' denied",
        call=EffectCall(kind="tool", target=tool.name),
        needs=outcome.needs,
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
    authority: Any = None,
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
        broker=ctx._runtime.broker,
        # Narrowed for this call when the caller asked for less; the workflow's
        # own authority otherwise. Passed rather than read off the context, so
        # two concurrent ctx.agent() calls under gather cannot see each other's.
        authority=authority if authority is not None else ctx._authority,
        deps=ctx.deps,
        workflow_ctx=ctx,
        spill=ctx._runtime.spill,
        hooks=ctx._runtime.hooks,
    )
    return await agent(input, settings=settings, context=context)
