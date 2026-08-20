"""Built-in agent runtime — the default executor that ships with the SDK.

The turn loop: prompt model → dispatch tool calls → check guardrails →
enforce budget → loop until final output or limit reached.

When called from within a workflow via ``ctx.agent()``, every model call
and tool call is journaled separately. An agent that dies on turn 9
resumes at turn 9 rather than re-paying for the first eight turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loom.agents.bounds import bound_result
from loom.agents.executor import AgentContext, AgentSettings
from loom.agents.guardrails import GuardrailAction
from loom.agents.limits import DEFAULT_LIMITS, UsageLimits
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
    ModelProvider,
    ModelRequest,
    ModelSettings,
    ToolSchema,
    estimate_cost,
    supports_native_output,
)
from loom.agents.output import FINAL_OUTPUT_TOOL, OutputMode, OutputSpec
from loom.agents.result import AgentResult, ItemKind, RunItem
from loom.agents.tools import Tool, ToolContext
from loom.core.exceptions import (
    AgentStopped,
    GuardrailTripwire,
    ModelBehaviorError,
    ModelRetry,
    UsageLimitExceeded,
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

    @dataclass(frozen=True)
    class _ToolText:
        """What one tool call contributed, in two forms.

        `raw` is what the tool returned, rendered; `bounded` is what the model
        is allowed to see. Kept apart because `ResultBounds` caps the
        conversation and never the journal — a replay has to reconstruct the
        run that happened.
        """

        raw: str
        bounded: str

    async def _dispatch_turn(
        self,
        calls: list[ToolCall],
        *,
        agent: Agent[Any],
        tool_map: dict[str, Tool],
        context: AgentContext | None,
        turn: int,
    ) -> list[BuiltInAgentRuntime._ToolText]:
        """Run every tool call in one turn, concurrently, in call order.

        The concurrency primitive is chosen by whether the agent is running
        *inside a workflow*. If it is, `ctx.gather` is used rather than
        `asyncio.gather`, because a tool that journals (`ctx.step` through
        `ToolContext.workflow_ctx`) must take its journal paths from a
        branch-local scope — the same reason `ctx.gather` exists at all. Using
        the raw primitive here would reintroduce the ordering defect one layer
        up, in code no workflow author wrote.

        Failures are collected rather than propagated per call: a tool that
        raised has an answer for the model ("Tool error: ..."), and one call
        blowing up must not discard the answers its siblings produced. A
        guardrail tripwire is the exception and is re-raised, because it means
        the run must stop.
        """
        if not calls:
            return []

        workflow_ctx = getattr(context, "workflow_ctx", None) if context else None
        work = [
            self._one_tool_call(
                call, agent=agent, tool_map=tool_map, context=context, turn=turn
            )
            for call in calls
        ]
        if workflow_ctx is not None and hasattr(workflow_ctx, "gather"):
            settled = await workflow_ctx.gather(*work, return_exceptions=True)
        else:
            settled = await asyncio.gather(*work, return_exceptions=True)

        for outcome in settled:
            if isinstance(outcome, GuardrailTripwire):
                raise outcome
        for outcome in settled:
            if isinstance(outcome, BaseException):
                raise outcome
        return list(settled)

    async def _one_tool_call(
        self,
        call: ToolCall,
        *,
        agent: Agent[Any],
        tool_map: dict[str, Tool],
        context: AgentContext | None,
        turn: int,
    ) -> BuiltInAgentRuntime._ToolText:
        """Guardrails, dispatch, logging and bounding for a single call."""
        tool = tool_map.get(call.name)
        if tool is None:
            message = f"Unknown tool '{call.name}'. Available: {list(tool_map)}"
            return self._ToolText(raw=message, bounded=message)

        for guard in agent.guardrails:
            verdict = await guard.evaluate(call.arguments, call.name)
            if verdict.action is GuardrailAction.TRIPWIRE:
                raise GuardrailTripwire(
                    verdict.message, guardrail_name=guard.name, info=verdict.info
                )
            if verdict.blocked:
                message = f"Guardrail rejected: {verdict.message}"
                return self._ToolText(raw=message, bounded=message)

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
            # Answered rather than raised: an agent told what it may not do can
            # pick another route, where a raised denial ends the run and loses
            # the turn's work. The refusal names the grant, so the message is
            # actionable to whoever reads the transcript too.
            result_str = str(denied)
            outcome = "denied"
        except ModelRetry as retry:
            result_str = str(retry)
            outcome = "retry"
        except Exception as exc:
            result_str = f"Tool error: {type(exc).__name__}: {exc}"
            outcome = "error"

        # One line per call, at DEBUG: what was asked, what came back, and how
        # long it took. An agent that loops does so invisibly otherwise — the
        # turn count says it happened, not what it was doing.
        tool_logger.debug(
            "turn=%d %s(%s) -> %s %s in %.0fms",
            turn,
            call.name,
            _brief(call.arguments),
            outcome,
            _brief(result_str, limit=160),
            (time.monotonic() - started) * 1000,
        )

        bounded = await bound_result(
            result_str,
            result if outcome == "ok" else None,
            bounds=agent.bounds,
            store=self._spill_store(context),
            run_id=context.run_id if context else "",
            tool=call.name,
            call_id=call.id,
        )
        return self._ToolText(raw=result_str, bounded=bounded)

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
            supports_native=supports_native_output(agent.model),
            has_tools=bool(tool_schemas),
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
                trim_history(
                context.history,
                max_messages=limits.max_history_messages,
                max_tokens=limits.max_history_tokens,
            )
            )
        messages.append(user(str(input)))

        # Turn loop
        cumulative_usage = Usage()
        all_tool_calls: list[ToolCall] = []
        items: list[RunItem] = []

        try:
            return await self._turns(
                model=agent.model,
                messages=messages,
                tool_schemas=tool_schemas,
                effective_tools=effective_tools,
                tool_map=tool_map,
                output_spec=output_spec,
                resolved_mode=resolved_mode,
                model_settings=model_settings,
                limits=limits,
                context=context,
                turns=turns,
                cumulative_usage=cumulative_usage,
                all_tool_calls=all_tool_calls,
                items=items,
            )
        except UsageLimitExceeded as exhausted:
            # The accounting is a local, and this exception unwinds past it.
            # Attaching it here is what lets a caller report what the attempt
            # cost instead of reporting zero.
            exhausted.usage = cumulative_usage
            exhausted.turns = len(
                [item for item in items if item.kind is ItemKind.MESSAGE]
            )
            exhausted.tool_calls = list(all_tool_calls)
            raise

    async def _turns(
        self,
        *,
        model: ModelProvider,
        messages: list[Message],
        tool_schemas: list[ToolSchema],
        effective_tools: list[Tool],
        tool_map: dict[str, Tool],
        output_spec: OutputSpec,
        resolved_mode: OutputMode,
        model_settings: ModelSettings,
        limits: UsageLimits,
        context: AgentContext | None,
        turns: _Turns | None,
        cumulative_usage: Usage,
        all_tool_calls: list[ToolCall],
        items: list[RunItem],
    ) -> AgentResult[Any]:
        """The loop itself, so the caller above can catch what it raises.

        Extracted only to give exhaustion somewhere to be caught without
        wrapping two hundred lines in an indent. Every mutable it needs is
        passed in rather than rebuilt, so what the caller attaches to the
        exception is the same object this was accumulating into.
        """
        agent = self._agent
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
                model=model.model_name,
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
            response = await model.complete(request)
            if turns is not None:
                await turns.after_model(turn, messages, response)
            cumulative_usage.add(response.usage)
            cumulative_usage.cost_usd = estimate_cost(
                response.model or model.model_name, cumulative_usage
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

                # Every tool call the model issued in this turn, dispatched
                # together. They are independent by construction — the model
                # asked for all of them before seeing any answer — so running
                # them one after another spent the sum of their latencies for
                # no reason. A turn calling four read operations was four round
                # trips of waiting.
                pending = [
                    call
                    for call in response.message.tool_calls
                    if call.name != FINAL_OUTPUT_TOOL
                ]
                all_tool_calls.extend(pending)
                for call in pending:
                    items.append(
                        RunItem(
                            kind=ItemKind.TOOL_CALL,
                            agent=agent.name,
                            name=call.name,
                            content=call.arguments,
                            turn=turn,
                        )
                    )

                outcomes = await self._dispatch_turn(
                    pending, agent=agent, tool_map=tool_map, context=context, turn=turn
                )

                # Appended in call order regardless of completion order, so the
                # transcript a replay reconstructs does not depend on which
                # tool happened to finish first.
                for call, outcome in zip(pending, outcomes, strict=True):
                    messages.append(
                        tool_result(call.id, outcome.bounded, name=call.name)
                    )
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
                        content=(
                            outcome.bounded
                            if agent.bounds is not None
                            else outcome.raw
                        ),
                        turn=turn,
                    ))
                limits.check_tool_calls(len(all_tool_calls))

                continue

            # No tool calls — check for text-based final output
            if response.finish_reason is FinishReason.STOP:
                text = response.message.text()
                if output_spec.is_structured and resolved_mode in (
                    OutputMode.PROMPTED,
                    # NATIVE was never handled here, so a provider that opted
                    # in would have had its schema-conforming JSON handed back
                    # as a raw string — the parse the whole mode exists for.
                    OutputMode.NATIVE,
                ):
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
