"""Workflow declaration.

A workflow is an ordinary async function whose first parameter is a
:class:`~loom.runtime.context.Context`. Everything that makes it durable lives
in metadata around the function rather than inside it, so the body stays readable, and
testable, as plain Python.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, overload

from loom.core.exceptions import ConfigurationError
from loom.core.ids import callable_name, code_fingerprint
from loom.core.retry import NO_RETRY, Retry
from loom.core.serde import resolve_annotations
from loom.core.types import Duration
from loom.runtime.context import Context
from loom.runtime.determinism import Diagnostic, warn_if_nondeterministic
from loom.runtime.flowcontrol import FlowControlPolicy
from loom.security.grants import GrantSet
from loom.triggers.base import TriggerSpec

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
DepsT = TypeVar("DepsT")

WorkflowFn = Callable[..., Awaitable[Any]]


@dataclass
class WorkflowDefinition(Generic[InputT, OutputT, DepsT]):
    """Everything the engine needs to run, resume, and expose one workflow."""

    fn: WorkflowFn
    name: str
    version: str = "1"
    description: str = ""
    triggers: tuple[TriggerSpec, ...] = ()
    input_type: Any = None
    output_type: Any = None
    deps_type: Any = None

    timeout: Duration | None = None
    retry: Retry = NO_RETRY
    """Retries of the *whole* orchestration. Prefer per-step retries; this is a backstop."""
    max_concurrent_runs: int | None = None
    on_failure: str | None = None
    """Name of a workflow to invoke when this one fails."""
    grants: GrantSet | None = None
    """Permissions this workflow may exercise.

    When set, ``ctx.agent()`` resolves only tools the grant set allows, and
    naming a denied toolset raises :class:`GrantDenied`. Leave ``None`` to
    impose no restriction — grants are opt-in, because an empty grant set
    means "nothing allowed", not "everything allowed".
    """
    flow_control: FlowControlPolicy | None = None
    """Admission policy evaluated before a run is created. Requires an
    ``AdmissionController`` on the Runtime."""

    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    takes_input: bool = field(default=True, init=False)
    code_hash: str = field(default="", init=False)
    diagnostics: list[Diagnostic] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not inspect.iscoroutinefunction(self.fn):
            raise ConfigurationError(
                f"workflow '{self.name}' must be an async function; orchestration is "
                f"cooperative and cannot block the event loop"
            )

        signature = inspect.signature(self.fn)
        params = list(signature.parameters.values())
        if not params:
            raise ConfigurationError(
                f"workflow '{self.name}' must accept a Context as its first parameter"
            )

        self.takes_input = len(params) > 1
        hints = resolve_annotations(self.fn)
        if self.input_type is None and self.takes_input:
            annotation = hints.get(params[1].name, params[1].annotation)
            self.input_type = None if annotation is inspect.Parameter.empty else annotation
        if self.output_type is None:
            annotation = hints.get("return", signature.return_annotation)
            self.output_type = None if annotation is inspect.Signature.empty else annotation
        if not self.description:
            self.description = inspect.cleandoc(self.fn.__doc__ or "").split("\n\n")[0]

        self.code_hash = code_fingerprint(self.fn)
        self.diagnostics = warn_if_nondeterministic(self.fn, workflow_name=self.name)

    async def invoke(self, ctx: Context[DepsT], input: Any) -> Any:
        """Call the body. Used by the engine; call the workflow directly in tests."""
        if self.takes_input:
            return await self.fn(ctx, input)
        return await self.fn(ctx)

    async def __call__(self, ctx: Context[DepsT], input: Any = None) -> Any:
        return await self.invoke(ctx, input)

    # -- introspection ----------------------------------------------------------------

    def triggers_of(self, kind: type[TriggerSpec]) -> list[TriggerSpec]:
        return [spec for spec in self.triggers if isinstance(spec, kind)]

    def input_schema(self) -> dict[str, Any] | None:
        """JSON Schema for the run input, or ``None`` if it cannot be derived.

        A caller that has to guess the shape wastes a run: handing
        ``{"email": "a@b.c"}`` to a body annotated ``email: str`` does not fail
        at the boundary, it fails several steps in with an AttributeError.
        Publishing the annotation is what lets the caller get it right first
        time, so this is worth surfacing wherever workflows are listed.
        """
        if not self.takes_input or self.input_type is None:
            return None

        from pydantic import TypeAdapter

        try:
            schema = dict(TypeAdapter(self.input_type).json_schema())
        except Exception:  # unrepresentable annotation — better silent than loud
            return None

        if not schema:  # bare `Any` says nothing a caller can use
            return None
        schema.setdefault("title", self.input_name)
        return schema

    @property
    def input_name(self) -> str:
        """Name of the input parameter, for error messages and schema titles."""
        params = list(inspect.signature(self.fn).parameters)
        return params[1] if len(params) > 1 else "input"

    def describe(self) -> dict[str, Any]:
        """Serializable manifest, used by the CLI, deploy tooling, and the dev UI."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "code_hash": self.code_hash,
            "tags": list(self.tags),
            "triggers": [spec.describe() for spec in self.triggers],
            "timeout": self.timeout,
            "on_failure": self.on_failure,
            "input_schema": self.input_schema(),
        }

    def __repr__(self) -> str:
        return f"<workflow {self.name} v{self.version}>"


@overload
def workflow(fn: WorkflowFn, /) -> WorkflowDefinition[Any, Any, Any]: ...


@overload
def workflow(
    *,
    name: str | None = ...,
    version: str = ...,
    triggers: Sequence[TriggerSpec] = ...,
    timeout: Duration | None = ...,
    retry: Retry = ...,
    max_concurrent_runs: int | None = ...,
    on_failure: str | None = ...,
    grants: GrantSet | None = ...,
    flow_control: FlowControlPolicy | None = ...,
    tags: tuple[str, ...] = ...,
    description: str = ...,
) -> Callable[[WorkflowFn], WorkflowDefinition[Any, Any, Any]]: ...


def workflow(
    fn: WorkflowFn | None = None,
    /,
    *,
    name: str | None = None,
    version: str = "1",
    triggers: Sequence[TriggerSpec] = (),
    timeout: Duration | None = None,
    retry: Retry = NO_RETRY,
    max_concurrent_runs: int | None = None,
    on_failure: str | None = None,
    grants: GrantSet | None = None,
    flow_control: FlowControlPolicy | None = None,
    tags: tuple[str, ...] = (),
    description: str = "",
) -> Any:
    """Declare a durable workflow.

    ```python
    @workflow(triggers=[Webhook("/orders"), Schedule("0 9 * * *")])
    async def fulfil(ctx: Context, order: Order) -> Receipt:
        stock = await ctx.step(reserve_stock, order.sku, order.quantity)
        if not stock.available:
            return Receipt.backordered(order)
        charge = await ctx.step(charge_card, order.customer, order.total)
        await ctx.sleep(timedelta(hours=1))
        return await ctx.step(ship, order, charge)
    ```
    """

    def decorate(target: WorkflowFn) -> WorkflowDefinition[Any, Any, Any]:
        return WorkflowDefinition(
            fn=target,
            name=name or callable_name(target, "anonymous_workflow"),
            version=version,
            description=description,
            triggers=tuple(triggers),
            timeout=timeout,
            retry=retry,
            max_concurrent_runs=max_concurrent_runs,
            on_failure=on_failure,
            grants=grants,
            flow_control=flow_control,
            tags=tags,
        )

    if fn is not None:
        return decorate(fn)
    return decorate
