"""Workflow declaration.

A workflow is an ordinary async function whose first parameter is a
:class:`~workflow_builder.runtime.context.Context`. Everything that makes it durable lives
in metadata around the function rather than inside it, so the body stays readable, and
testable, as plain Python.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, overload

from workflow_builder.core.exceptions import ConfigurationError
from workflow_builder.core.ids import code_fingerprint
from workflow_builder.core.retry import NO_RETRY, Retry
from workflow_builder.core.types import Duration
from workflow_builder.runtime.context import Context
from workflow_builder.runtime.determinism import Diagnostic, warn_if_nondeterministic
from workflow_builder.triggers.base import TriggerSpec

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
        if self.input_type is None and self.takes_input:
            annotation = params[1].annotation
            self.input_type = None if annotation is inspect.Parameter.empty else annotation
        if self.output_type is None:
            annotation = signature.return_annotation
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
            name=name or getattr(target, "__name__", "anonymous_workflow"),
            version=version,
            description=description,
            triggers=tuple(triggers),
            timeout=timeout,
            retry=retry,
            max_concurrent_runs=max_concurrent_runs,
            on_failure=on_failure,
            tags=tags,
        )

    if fn is not None:
        return decorate(fn)
    return decorate
