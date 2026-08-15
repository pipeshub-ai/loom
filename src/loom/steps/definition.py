"""Step declaration: the unit of durable, retryable side effect.

Steps come in three classes that form a "determinism dial":

- ``@pure``: Deterministic transforms. No I/O. Recomputed on replay (free).
  The engine never journals output — it just calls the function again.
- ``@effect``: Side-effecting I/O. Journaled. Memoized on replay.
  This is the default and what ``@step`` creates.
- ``@node``: Generic code node. Treated like an effect for durability purposes.
  Exists so custom/opaque nodes are visually distinct in WGIR.

The ``@step`` decorator is an alias for ``@effect`` for backward compatibility.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, ParamSpec, TypeVar, overload

from loom.core.exceptions import ConfigurationError
from loom.core.ids import code_fingerprint, stable_hash
from loom.core.retry import DEFAULT_RETRY, NO_RETRY, OnError, Retry
from loom.core.serde import resolve_annotations
from loom.core.types import Duration, to_seconds
from loom.steps.context import StepContext

P = ParamSpec("P")
R = TypeVar("R")


class StepClass(StrEnum):
    """The determinism dial: how a step interacts with the journal."""

    PURE = "pure"
    """Deterministic transform. No I/O. Recomputed on replay — never journaled."""
    EFFECT = "effect"
    """Side-effecting I/O. Journaled and memoized on replay. The default."""
    NODE = "node"
    """Generic code node. Treated as effect for durability. Distinct in WGIR."""
    AGENT = "agent"
    """Reserved for Phase 2. Agent steps with multi-turn tool use."""


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Memoize a step's result across runs.

    ``key`` defaults to the step's content address (name + arguments), which is usually
    what you want for pure-ish lookups such as geocoding or embedding.
    """

    ttl: Duration = 3600.0
    key: Callable[..., str] | None = None
    scope: str = "global"
    """``"global"`` shares the cache across runs; ``"run"`` scopes it to one execution."""


@dataclass
class StepDefinition(Generic[P, R]):
    """A callable wrapped with durability metadata.

    Instances stay directly callable, so a step is still an ordinary async function you
    can unit test without any engine in the picture.
    """

    fn: Callable[..., Awaitable[R]]
    name: str
    klass: StepClass = StepClass.EFFECT
    """The step class — pure, effect, node, or agent."""
    retry: Retry = DEFAULT_RETRY
    timeout: Duration | None = None
    on_error: OnError = OnError.RAISE
    fallback: Any = None
    cache: CachePolicy | None = None
    output_type: Any = None
    concurrency_key: str | None = None
    """Steps sharing a key contend for the same semaphore, protecting fragile upstreams."""
    max_concurrency: int | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    idempotency: Callable[..., str] | None = None
    """Optional function to derive an idempotency key from step arguments."""
    wants_context: bool = field(default=False, init=False)
    code_hash: str = field(default="", init=False)
    contract_hash: str = field(default="", init=False)
    """Hash of the Pydantic schema of input/output types — detects contract changes."""
    closure_hash: str = field(default="", init=False)
    """Hash of the transitive function body — detects implementation changes."""

    def __post_init__(self) -> None:
        if not asyncio.iscoroutinefunction(self.fn) and not inspect.isasyncgenfunction(self.fn):
            # Sync functions are supported but must be offloaded, which we do at call time.
            pass
        self.wants_context = _wants_step_context(self.fn)
        self.code_hash = code_fingerprint(self.fn)
        self.closure_hash = self.code_hash  # alias — same value for now
        self.contract_hash = _compute_contract_hash(self.fn)
        if self.output_type is None:
            self.output_type = resolve_annotations(self.fn).get("return")
        if not self.description:
            self.description = inspect.cleandoc(self.fn.__doc__ or "").split("\n\n")[0]

    @property
    def is_pure(self) -> bool:
        """True if this step is recomputed on replay (never journaled)."""
        return self.klass == StepClass.PURE

    @property
    def is_journaled(self) -> bool:
        """True if this step's output is recorded in the journal."""
        return self.klass != StepClass.PURE

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Invoke the underlying function directly, bypassing the journal."""
        return await self.invoke(None, *args, **kwargs)

    async def invoke(self, ctx: StepContext | None, /, *args: Any, **kwargs: Any) -> R:
        """Run the body once, injecting :class:`StepContext` when the signature wants it."""
        call_args = (ctx, *args) if self.wants_context else args
        if self.wants_context and ctx is None:
            call_args = (_detached_context(self.name), *args)

        if asyncio.iscoroutinefunction(self.fn):
            coro = self.fn(*call_args, **kwargs)
        else:
            loop = asyncio.get_running_loop()
            coro = loop.run_in_executor(None, lambda: self.fn(*call_args, **kwargs))  # type: ignore[arg-type,return-value]

        if self.timeout is None:
            return await coro  # type: ignore[no-any-return]
        return await asyncio.wait_for(coro, to_seconds(self.timeout))  # type: ignore[no-any-return]

    def with_options(
        self,
        *,
        retry: Retry | None = None,
        timeout: Duration | None = None,
        on_error: OnError | None = None,
        fallback: Any = None,
        name: str | None = None,
    ) -> StepDefinition[P, R]:
        """Return a variant with overridden policy, leaving the original untouched."""
        clone = StepDefinition(
            fn=self.fn,
            name=name or self.name,
            klass=self.klass,
            retry=retry or self.retry,
            timeout=self.timeout if timeout is None else timeout,
            on_error=on_error or self.on_error,
            fallback=self.fallback if fallback is None else fallback,
            cache=self.cache,
            output_type=self.output_type,
            concurrency_key=self.concurrency_key,
            max_concurrency=self.max_concurrency,
            description=self.description,
            tags=self.tags,
            idempotency=self.idempotency,
        )
        return clone

    def __repr__(self) -> str:
        return f"<{self.klass.value} {self.name}>"


def _compute_contract_hash(fn: Callable[..., Any]) -> str:
    """Derive a hash from the function's type annotations (input/output contract).

    Changes when argument types or return type change — the journal should be
    invalidated because the data shape is different.
    """
    try:
        sig = inspect.signature(fn)
        contract_parts: list[str] = []
        for pname, param in sig.parameters.items():
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                ann_str = "Any"
            elif isinstance(ann, type):
                ann_str = f"{ann.__module__}.{ann.__qualname__}"
            else:
                ann_str = str(ann)
            contract_parts.append(f"{pname}:{ann_str}")
        ret = sig.return_annotation
        if ret is inspect.Signature.empty:
            contract_parts.append("return:Any")
        elif isinstance(ret, type):
            contract_parts.append(f"return:{ret.__module__}.{ret.__qualname__}")
        else:
            contract_parts.append(f"return:{ret}")
        return stable_hash("|".join(contract_parts))
    except (TypeError, ValueError):
        return ""


def _wants_step_context(fn: Callable[..., Any]) -> bool:
    """A step opts into :class:`StepContext` by annotating or naming its first parameter."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if not params:
        return False
    first = params[0]
    if first.annotation is StepContext:
        return True
    annotation = first.annotation
    if isinstance(annotation, str) and annotation.split(".")[-1] == "StepContext":
        return True
    return first.name == "ctx" and annotation is inspect.Parameter.empty


def _detached_context(step_name: str) -> StepContext:
    return StepContext(run_id="detached", workflow="detached", step_name=step_name, path="")


@overload
def step(fn: Callable[P, Awaitable[R]], /) -> StepDefinition[P, R]: ...


@overload
def step(
    *,
    name: str | None = ...,
    retry: Retry | int | None = ...,
    timeout: Duration | None = ...,
    on_error: OnError = ...,
    fallback: Any = ...,
    cache: CachePolicy | None = ...,
    concurrency_key: str | None = ...,
    max_concurrency: int | None = ...,
    tags: tuple[str, ...] = ...,
) -> Callable[[Callable[P, Awaitable[R]]], StepDefinition[P, R]]: ...


def step(
    fn: Callable[P, Awaitable[R]] | None = None,
    /,
    *,
    name: str | None = None,
    retry: Retry | int | None = None,
    timeout: Duration | None = None,
    on_error: OnError = OnError.RAISE,
    fallback: Any = None,
    cache: CachePolicy | None = None,
    concurrency_key: str | None = None,
    max_concurrency: int | None = None,
    tags: tuple[str, ...] = (),
) -> Any:
    """Declare a durable step.

    A step is the boundary between deterministic orchestration and the messy outside
    world. Everything that talks to a network, a disk, a clock, or a model belongs in one,
    because only steps are journaled — and only journaled work survives a crash.

    ```python
    @step(retry=Retry(max_attempts=5), timeout=30)
    async def charge_card(customer_id: str, cents: int) -> Charge:
        return await stripe.charges.create(customer=customer_id, amount=cents)
    ```
    """

    resolved_retry = (
        DEFAULT_RETRY
        if retry is None
        else Retry(max_attempts=retry)
        if isinstance(retry, int)
        else retry
    )

    def decorate(target: Callable[P, Awaitable[R]]) -> StepDefinition[P, R]:
        if isinstance(target, StepDefinition):
            raise ConfigurationError(f"{target.name} is already a step")
        return StepDefinition(
            fn=target,
            name=name or getattr(target, "__name__", "anonymous_step"),
            retry=resolved_retry,
            timeout=timeout,
            on_error=on_error,
            fallback=fallback,
            cache=cache,
            concurrency_key=concurrency_key,
            max_concurrency=max_concurrency,
            tags=tags,
        )

    if fn is not None:
        return decorate(fn)
    return decorate


# ---------------------------------------------------------------------------
# Typed decorator aliases — the "determinism dial"
# ---------------------------------------------------------------------------


@overload
def pure(fn: Callable[P, Awaitable[R]], /) -> StepDefinition[P, R]: ...


@overload
def pure(
    *,
    name: str | None = ...,
    cache: CachePolicy | None = ...,
    tags: tuple[str, ...] = ...,
) -> Callable[[Callable[P, Awaitable[R]]], StepDefinition[P, R]]: ...


def pure(
    fn: Callable[P, Awaitable[R]] | None = None,
    /,
    *,
    name: str | None = None,
    cache: CachePolicy | None = None,
    tags: tuple[str, ...] = (),
) -> Any:
    """Declare a **pure** step — deterministic transform, no I/O.

    Pure steps are recomputed on replay rather than memoized from the journal.
    They never write to the journal, making them free during replay. Use for
    data transformations, formatting, validation, and other side-effect-free work.

    ```python
    @pure
    async def format_address(data: dict) -> str:
        return f"{data['street']}, {data['city']}, {data['zip']}"
    ```
    """

    def decorate(target: Callable[P, Awaitable[R]]) -> StepDefinition[P, R]:
        if isinstance(target, StepDefinition):
            raise ConfigurationError(f"{target.name} is already a step")
        return StepDefinition(
            fn=target,
            name=name or getattr(target, "__name__", "anonymous_step"),
            klass=StepClass.PURE,
            retry=NO_RETRY,
            cache=cache,
            tags=tags,
        )

    if fn is not None:
        return decorate(fn)
    return decorate


@overload
def effect(fn: Callable[P, Awaitable[R]], /) -> StepDefinition[P, R]: ...


@overload
def effect(
    *,
    name: str | None = ...,
    retry: Retry | int | None = ...,
    timeout: Duration | None = ...,
    on_error: OnError = ...,
    fallback: Any = ...,
    cache: CachePolicy | None = ...,
    concurrency_key: str | None = ...,
    max_concurrency: int | None = ...,
    idempotency: Callable[..., str] | None = ...,
    tags: tuple[str, ...] = ...,
) -> Callable[[Callable[P, Awaitable[R]]], StepDefinition[P, R]]: ...


def effect(
    fn: Callable[P, Awaitable[R]] | None = None,
    /,
    *,
    name: str | None = None,
    retry: Retry | int | None = None,
    timeout: Duration | None = None,
    on_error: OnError = OnError.RAISE,
    fallback: Any = None,
    cache: CachePolicy | None = None,
    concurrency_key: str | None = None,
    max_concurrency: int | None = None,
    idempotency: Callable[..., str] | None = None,
    tags: tuple[str, ...] = (),
) -> Any:
    """Declare an **effect** step — side-effecting I/O, journaled and memoized.

    This is the most common step class. API calls, database writes, file I/O,
    and anything that should not be re-executed on replay belongs here.

    ```python
    @effect(retry=Retry(max_attempts=3), timeout=30)
    async def charge_card(customer_id: str, cents: int) -> dict:
        return await stripe.charges.create(customer=customer_id, amount=cents)
    ```
    """
    resolved_retry = (
        DEFAULT_RETRY
        if retry is None
        else Retry(max_attempts=retry)
        if isinstance(retry, int)
        else retry
    )

    def decorate(target: Callable[P, Awaitable[R]]) -> StepDefinition[P, R]:
        if isinstance(target, StepDefinition):
            raise ConfigurationError(f"{target.name} is already a step")
        return StepDefinition(
            fn=target,
            name=name or getattr(target, "__name__", "anonymous_step"),
            klass=StepClass.EFFECT,
            retry=resolved_retry,
            timeout=timeout,
            on_error=on_error,
            fallback=fallback,
            cache=cache,
            concurrency_key=concurrency_key,
            max_concurrency=max_concurrency,
            idempotency=idempotency,
            tags=tags,
        )

    if fn is not None:
        return decorate(fn)
    return decorate


@overload
def node(fn: Callable[P, Awaitable[R]], /) -> StepDefinition[P, R]: ...


@overload
def node(
    *,
    name: str | None = ...,
    retry: Retry | int | None = ...,
    timeout: Duration | None = ...,
    on_error: OnError = ...,
    tags: tuple[str, ...] = ...,
) -> Callable[[Callable[P, Awaitable[R]]], StepDefinition[P, R]]: ...


def node(
    fn: Callable[P, Awaitable[R]] | None = None,
    /,
    *,
    name: str | None = None,
    retry: Retry | int | None = None,
    timeout: Duration | None = None,
    on_error: OnError = OnError.RAISE,
    tags: tuple[str, ...] = (),
) -> Any:
    """Declare a **node** step — generic code node, treated as effect for durability.

    Nodes are visually distinct in the WGIR graph but behave identically to effects
    for journaling purposes. Use for custom/opaque processing blocks.

    ```python
    @node
    async def custom_transform(data: dict) -> dict:
        # complex business logic
        return transformed_data
    ```
    """
    resolved_retry = (
        DEFAULT_RETRY
        if retry is None
        else Retry(max_attempts=retry)
        if isinstance(retry, int)
        else retry
    )

    def decorate(target: Callable[P, Awaitable[R]]) -> StepDefinition[P, R]:
        if isinstance(target, StepDefinition):
            raise ConfigurationError(f"{target.name} is already a step")
        return StepDefinition(
            fn=target,
            name=name or getattr(target, "__name__", "anonymous_step"),
            klass=StepClass.NODE,
            retry=resolved_retry,
            timeout=timeout,
            on_error=on_error,
            tags=tags,
        )

    if fn is not None:
        return decorate(fn)
    return decorate
