"""Tools: the agent's hands.

A tool is an ordinary typed Python function. Its JSON Schema is derived from the
signature, its description from the docstring, and its argument descriptions from the
docstring's ``Args:`` section — so there is no second, drifting copy of the contract.

Crucially, a ``@step`` and a whole workflow are both convertible to tools. That is the
code-first answer to n8n's four-hundred integration nodes: anything you already wrote as a
durable step is already an agent tool, with retries and journaling intact.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, overload

from pydantic import ConfigDict, Field, TypeAdapter, create_model

from loom.core.exceptions import ConfigurationError, ModelRetry
from loom.core.ids import callable_name
from loom.core.serde import encode

if TYPE_CHECKING:
    from loom.runtime.context import Context
    from loom.runtime.workflow import WorkflowDefinition
    from loom.steps.definition import StepDefinition

ApprovalRule = "bool | Callable[[dict[str, Any]], bool | Awaitable[bool]]"


@dataclass
class ToolContext:
    """What a tool can see about the call it is serving."""

    run_id: str = ""
    agent_name: str = ""
    tool_call_id: str = ""
    deps: Any = None
    workflow_ctx: Context[Any] | None = None
    """The durable context, when the agent is running inside a workflow.

    Present so a tool can itself perform durable work — journaled sub-steps, child
    workflows, or its own approval gates.
    """
    attempt: int = 1
    approved: bool = True


@dataclass
class Tool:
    """A callable exposed to a model, with a schema it can be trusted to fill."""

    fn: Callable[..., Any]
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    takes_context: bool = False
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False
    """``True`` always asks a human; a predicate asks only for risky arguments."""
    max_retries: int = 2
    """How many times a :class:`ModelRetry` from this tool is fed back before failing."""
    strict: bool = True
    validator: TypeAdapter[Any] | None = None
    output_type: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        from loom.agents.models import ToolSchema

        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            strict=self.strict,
        ).model_dump()

    def requires_approval(self, arguments: dict[str, Any]) -> bool:
        """Decide whether this call needs a human.

        Fails closed: if a predicate raises, we ask for approval rather than assume it is
        safe. A crash in a risk check must not become an authorisation.
        """
        if isinstance(self.needs_approval, bool):
            return self.needs_approval
        try:
            return bool(self.needs_approval(arguments))
        except Exception:
            return True

    def enforce_approval(self, arguments: dict[str, Any]) -> None:
        """Raise if this tool needs a human for *these* arguments.

        Called from every path that reaches the underlying callable — the
        built-in agent loop, and each backend adapter, which hands
        :attr:`fn` to a third-party framework that knows nothing about LOOM.
        Missing one adapter would make the gate depend on which backend a
        deployment happens to use, which is worse than not having it.
        """
        if not self.requires_approval(arguments):
            return
        from loom.runtime.effects import EffectCall, EffectDenied

        raise EffectDenied(
            f"tool '{self.name}' declares needs_approval for these arguments",
            call=EffectCall(kind="tool", target=self.name),
            needs="approval",
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Coerce model-supplied arguments, raising :class:`ModelRetry` on mismatch.

        Validation errors go back to the model as text rather than failing the run,
        because a malformed tool call is usually one corrective turn away from working.
        """
        if self.validator is None:
            return arguments
        try:
            model = self.validator.validate_python(arguments)
        except Exception as exc:
            raise ModelRetry(
                f"Invalid arguments for tool '{self.name}': {exc}. "
                f"Call it again with arguments matching this schema: "
                f"{json.dumps(self.parameters)}"
            ) from exc
        return {name: getattr(model, name) for name in type(model).model_fields}

    async def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        coerced = self.validate_arguments(arguments)
        call_args = (ctx,) if self.takes_context else ()
        result = self.fn(*call_args, **coerced)
        if inspect.isawaitable(result):
            result = await result
        return result

    def render_result(self, value: Any) -> str:
        """Format a tool's return value for the conversation."""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(encode(value), ensure_ascii=False)
        except Exception:
            return str(value)

    def __repr__(self) -> str:
        return f"<tool {self.name}>"


# --------------------------------------------------------------------------------------
# Schema derivation
# --------------------------------------------------------------------------------------

_ARGS_SECTION = re.compile(r"^\s*(Args|Arguments|Parameters):\s*$", re.MULTILINE)
_ARG_LINE = re.compile(r"^\s+(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$")


def parse_docstring(fn: Callable[..., Any]) -> tuple[str, dict[str, str]]:
    """Split a docstring into a summary and per-argument descriptions."""
    raw = inspect.getdoc(fn) or ""
    if not raw:
        return "", {}

    match = _ARGS_SECTION.search(raw)
    if match is None:
        return raw.split("\n\n")[0].strip(), {}

    summary = raw[: match.start()].split("\n\n")[0].strip()
    descriptions: dict[str, str] = {}
    current: str | None = None
    for line in raw[match.end() :].splitlines():
        if not line.strip():
            if descriptions:
                break
            continue
        if re.match(r"^\s*(Returns|Raises|Yields|Examples?|Note)s?:\s*$", line):
            break
        found = _ARG_LINE.match(line)
        if found:
            current = found.group(1).lstrip("*")
            descriptions[current] = found.group(2).strip()
        elif current:
            descriptions[current] += " " + line.strip()
    return summary, descriptions


def build_parameter_schema(
    fn: Callable[..., Any], *, skip_first: bool = False
) -> tuple[dict[str, Any], TypeAdapter[Any] | None]:
    """Derive a JSON Schema and a validator from a function signature."""
    signature = inspect.signature(fn)
    _, arg_docs = parse_docstring(fn)

    parameters = list(signature.parameters.values())
    if skip_first and parameters:
        parameters = parameters[1:]

    fields: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        annotation = (
            Any if parameter.annotation is inspect.Parameter.empty else parameter.annotation
        )
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (
            annotation,
            Field(default, description=arg_docs.get(parameter.name, "")),
        )

    if not fields:
        return {"type": "object", "properties": {}, "additionalProperties": False}, None

    # ``__module__`` is the tool's, not this one's. These modules use postponed
    # annotations, so pydantic resolves ``list[Attachment] | None`` as a string
    # against the namespace of the module the model claims — and by default
    # that is *this* file, which has never heard of the tool's types. The
    # resulting model cannot be built at all: ``gmail_send_message`` raised
    # "GmailSendMessageArguments is not fully defined" and took
    # ``resolve_tools(["gmail"])`` down with it, so an agent handed that
    # toolset failed before its first turn.
    model = create_model(
        f"{getattr(fn, '__name__', 'tool').title().replace('_', '')}Arguments",
        __config__=ConfigDict(extra="forbid", arbitrary_types_allowed=True),
        __module__=getattr(fn, "__module__", __name__),
        **fields,
    )
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.setdefault("additionalProperties", False)
    return schema, TypeAdapter(model)


def _wants_tool_context(fn: Callable[..., Any]) -> bool:
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if not parameters:
        return False
    annotation = parameters[0].annotation
    if annotation is ToolContext:
        return True
    if isinstance(annotation, str) and annotation.split(".")[-1] == "ToolContext":
        return True
    return parameters[0].name in ("ctx", "context") and annotation is inspect.Parameter.empty


# --------------------------------------------------------------------------------------
# Decorator and adapters
# --------------------------------------------------------------------------------------


@overload
def tool(fn: Callable[..., Any], /) -> Tool: ...


@overload
def tool(
    *,
    name: str | None = ...,
    description: str | None = ...,
    needs_approval: bool | Callable[[dict[str, Any]], bool] = ...,
    max_retries: int = ...,
    strict: bool = ...,
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False,
    max_retries: int = 2,
    strict: bool = True,
) -> Any:
    """Turn a typed function into an agent tool.

    ```python
    @tool(needs_approval=lambda args: args["amount_cents"] > 50_00)
    async def refund(order_id: str, amount_cents: int) -> str:
        \"\"\"Refund part or all of an order.

        Args:
            order_id: The order to refund.
            amount_cents: How much to refund, in cents.
        \"\"\"
        return await payments.refund(order_id, amount_cents)
    ```
    """

    def decorate(target: Callable[..., Any]) -> Tool:
        takes_context = _wants_tool_context(target)
        schema, validator = build_parameter_schema(target, skip_first=takes_context)
        summary, _ = parse_docstring(target)
        return Tool(
            fn=target,
            name=name or callable_name(target, "tool"),
            description=description or summary,
            parameters=schema,
            takes_context=takes_context,
            needs_approval=needs_approval,
            max_retries=max_retries,
            strict=strict,
            validator=validator,
            output_type=getattr(target, "__annotations__", {}).get("return"),
        )

    if fn is not None:
        return decorate(fn)
    return decorate


def tool_from_step(
    definition: StepDefinition[Any, Any],
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False,
) -> Tool:
    """Expose an existing durable step as a tool.

    The step keeps its retry policy and stays journaled, so a model-initiated call is
    exactly as reliable as one written by hand in orchestration code.
    """
    schema, validator = build_parameter_schema(definition.fn, skip_first=definition.wants_context)
    summary, _ = parse_docstring(definition.fn)

    async def invoke(ctx: ToolContext, **kwargs: Any) -> Any:
        if ctx.workflow_ctx is not None:
            return await ctx.workflow_ctx.step(definition, **kwargs)
        return await definition(**kwargs)

    return Tool(
        fn=invoke,
        name=name or definition.name,
        description=description or summary or definition.description,
        parameters=schema,
        takes_context=True,
        needs_approval=needs_approval,
        validator=validator,
        output_type=definition.output_type,
        metadata={"step": definition.name},
    )


def tool_from_workflow(
    definition: WorkflowDefinition[Any, Any, Any],
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False,
) -> Tool:
    """Expose a whole workflow as a tool, so agents can invoke durable sub-processes."""
    input_type = definition.input_type
    if input_type is None:
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": {"input": {"description": "Input payload for the workflow"}},
            "required": ["input"],
        }
        validator = None
    else:
        adapter = TypeAdapter(input_type)
        inner = adapter.json_schema()
        parameters = {
            "type": "object",
            "properties": {"input": inner},
            "required": ["input"],
            "additionalProperties": False,
        }
        validator = None

    async def invoke(ctx: ToolContext, input: Any = None) -> Any:
        if ctx.workflow_ctx is None:
            raise ConfigurationError(
                f"workflow tool '{definition.name}' can only be used by an agent running "
                f"inside a workflow, because it needs a durable context to start a child run"
            )
        return await ctx.workflow_ctx.child(definition, input)

    return Tool(
        fn=invoke,
        name=name or definition.name,
        description=description or definition.description,
        parameters=parameters,
        takes_context=True,
        needs_approval=needs_approval,
        validator=validator,
        output_type=definition.output_type,
        metadata={"workflow": definition.name},
    )


def coerce_tool(candidate: Any) -> Tool:
    """Accept a :class:`Tool`, a step, a workflow, or a plain function."""
    from loom.runtime.workflow import WorkflowDefinition as _Workflow
    from loom.steps.definition import StepDefinition as _Step

    if isinstance(candidate, Tool):
        return candidate
    if isinstance(candidate, _Step):
        return tool_from_step(candidate)
    if isinstance(candidate, _Workflow):
        return tool_from_workflow(candidate)
    if callable(candidate):
        return tool(candidate)
    raise ConfigurationError(f"cannot use {candidate!r} as a tool")
