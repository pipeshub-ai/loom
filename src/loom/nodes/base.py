"""The node contract: Pydantic in, Pydantic out.

A node is a **packaging and contract layer over the existing engine, not a
second durability mechanism.** Everything durable a node does goes through
:class:`Context`; ``ctx.node(...)`` produces the same journal entries the
equivalent hand-written code would. A node that parks a run raises ``Suspend``
exactly as ``ctx.wait_for_approval`` does.

The test for the whole design is that deleting this package could not change how
any existing workflow replays.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from random import Random
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from loom.core.exceptions import ConfigurationError
from loom.nodes.errors import NodeContractError
from loom.nodes.spec import NodeSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from loom.runtime.context import Context

__all__ = ["In", "Node", "NodeContext", "Out", "derive_spec", "validate_node_class"]

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class NodeContext:
    """The subset of :class:`Context` a node body may use.

    A real object rather than a ``Protocol``, because the narrowing is a
    security boundary and a typing-level one enforces nothing at runtime. A node
    is third-party code running inside somebody's workflow: it may do durable
    work, wait, and report, but it may not restructure the run it is a part of.

    Deliberately absent, each for the same reason — they are the *workflow's*
    prerogative and a node that could reach them can change a run its author
    never saw:

    ``continue_as_new`` (ends the run), ``child`` (spawns under the parent's
    identity), ``publish``/``signal`` (broadcasts as the parent), ``state``
    (shared across every run of the workflow), the artifact API (writes under
    the parent's names), and ``compensate`` (edits the parent's unwind stack).
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: Context[Any]) -> None:
        self._ctx = ctx

    # -- identity -----------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._ctx.run_id

    @property
    def workflow(self) -> str:
        return self._ctx.workflow

    @property
    def attempt(self) -> int:
        return self._ctx.attempt

    @property
    def deps(self) -> Any:
        return self._ctx.deps

    @property
    def usage(self) -> Any:
        return self._ctx.usage

    @property
    def logger(self) -> Any:
        return self._ctx.logger

    # -- durable work -------------------------------------------------------

    def step(self, target: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Journal a step. The node's own I/O goes through here or not at all."""
        return self._ctx.step(target, *args, **kwargs)

    def call(self, name: str, fn: Callable[[], Awaitable[Any]], **kwargs: Any) -> Any:
        return self._ctx.call(name, fn, **kwargs)

    def agent(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate judgement to a model. What ``agent.*`` nodes are built on."""
        return self._ctx.agent(*args, **kwargs)

    async def gather(self, *args: Any, **kwargs: Any) -> Any:
        return await self._ctx.gather(*args, **kwargs)

    # -- time and waiting ---------------------------------------------------

    async def sleep(self, duration: Any, *, name: str = "sleep") -> None:
        await self._ctx.sleep(duration, name=name)

    async def sleep_until(self, when: datetime, *, name: str = "sleep") -> None:
        await self._ctx.sleep_until(when, name=name)

    async def wait_for_event(self, name: str, **kwargs: Any) -> Any:
        """Park until an event arrives. How every ``human.*`` node waits."""
        return await self._ctx.wait_for_event(name, **kwargs)

    # -- deterministic reads ------------------------------------------------

    def now(self) -> datetime:
        return self._ctx.now()

    def uuid4(self) -> str:
        return self._ctx.uuid4()

    def random(self) -> Random:
        return self._ctx.random()

    # -- declared capabilities ----------------------------------------------

    def capability(self, name: str) -> Any:
        """The Runtime capability *name*, which the node declared in ``requires``.

        The one hole in the narrowing, and a deliberately small one: a node may
        reach the specific things it declared a dependency on, and nothing else.
        Reaching them through the Runtime directly would hand a node everything
        else on it too.
        """
        attribute = _CAPABILITY_ATTRS.get(name) or name
        value = getattr(self._ctx._runtime, attribute, None)
        if value is None:

            raise ConfigurationError(
                f"this Runtime has no {name!r} configured. A node that needs one "
                "must list it in NodeSpec.requires, which is checked before the "
                "node runs."
            )
        return value

    # -- output -------------------------------------------------------------

    async def report(self, message: str, *, kind: str = "text") -> None:
        await self._ctx.report(message, kind=kind)

    def __repr__(self) -> str:
        return f"<NodeContext run={self._ctx.run_id}>"


class Node(Generic[In, Out]):
    """A typed, versioned, catalogued unit of workflow work.

    Subclasses declare three things and implement one method::

        @register_node
        class LeadScoreNode(Node[ScoreIn, ScoreOut]):
            spec = NodeSpec(id="custom.lead_score", category=NodeCategory.TRANSFORM,
                            summary="Score a lead from its description text.")
            Input, Output = ScoreIn, ScoreOut

            async def run(self, ctx, payload: ScoreIn) -> ScoreOut:
                score = await ctx.step(compute_score, payload.text)
                return ScoreOut(score=score, passed=score >= payload.threshold)

    That is the whole integration. Everything the coding agent sees — schemas,
    import line, the rendered call — is derived from this class, so a node is
    discoverable without a second declaration anywhere.
    """

    spec: ClassVar[NodeSpec]
    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]

    async def run(self, ctx: NodeContext, payload: Any) -> Any:
        raise NotImplementedError

    # -- capability declaration --------------------------------------------

    @classmethod
    def missing_requirements(cls, runtime: Any) -> list[str]:
        """Which of ``spec.requires`` this runtime does not satisfy.

        Overridable, because only the node knows what its requirement names
        mean. Checked at resolution, before any body runs.
        """
        missing: list[str] = []
        for capability in cls.spec.requires:
            attribute = _CAPABILITY_ATTRS.get(capability) or capability
            if getattr(runtime, attribute, None) is None:
                missing.append(capability)
        return missing

    def __repr__(self) -> str:
        return f"<node {type(self).spec.id}@{type(self).spec.version}>"


#: Capability name to the ``Runtime`` attribute that satisfies it. A mapping and
#: not a convention, so a rename of either side is a change in one place.
_CAPABILITY_ATTRS: dict[str, str] = {"human_channel": "human"}


# ---------------------------------------------------------------------------
# Registration-time validation
# ---------------------------------------------------------------------------


def validate_node_class(cls: type[Node[Any, Any]]) -> None:
    """Raise :class:`NodeContractError` if *cls* is not a usable node.

    At registration, where the author sees it — not at resolution inside
    somebody else's run, and not as an ``AttributeError`` five frames into a
    workflow that looked fine when it was generated.
    """
    name = getattr(cls, "__name__", repr(cls))

    if not isinstance(getattr(cls, "spec", None), NodeSpec):
        raise NodeContractError(f"{name} has no NodeSpec assigned to `spec`")

    spec = cls.spec
    if "." not in spec.id:
        raise NodeContractError(
            f"node id {spec.id!r} is not namespaced — use '<namespace>.<name>', "
            "e.g. 'custom.lead_score'. A flat id collides the moment two "
            "packages pick the same word."
        )

    for role in ("Input", "Output"):
        model = getattr(cls, role, None)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise NodeContractError(
                f"{name}.{role} must be a pydantic BaseModel subclass, got "
                f"{model!r}. The models are the node's public contract: the "
                "journal needs them serializable and the coding agent needs "
                "them as schema."
            )

    run = cls.__dict__.get("run") or getattr(cls, "run", None)
    if run is Node.run:
        raise NodeContractError(f"{name} does not implement run()")
    if not inspect.iscoroutinefunction(run):
        raise NodeContractError(
            f"{name}.run must be `async def` — a node body does durable work and "
            "the engine awaits it."
        )

    parameters = list(inspect.signature(run).parameters)
    if len(parameters) < 3:
        raise NodeContractError(
            f"{name}.run must take (self, ctx, payload); got ({', '.join(parameters)})"
        )


def derive_spec(cls: type[Node[Any, Any]]) -> NodeSpec:
    """Fill in everything a node should not have to write twice.

    ``node_class``, ``input_schema``, and ``output_schema`` come from the class.
    A declared value is left alone, so a node can override a schema where the
    generated one is unhelpful — but nothing has to.
    """
    validate_node_class(cls)
    spec = cls.spec
    updates: dict[str, Any] = {}

    if not spec.node_class:
        updates["node_class"] = f"{cls.__module__}:{cls.__qualname__}"
    if not spec.input_schema:
        updates["input_schema"] = cls.Input.model_json_schema()
    if not spec.output_schema:
        updates["output_schema"] = cls.Output.model_json_schema()
    if not spec.summary and cls.__doc__:
        updates["summary"] = inspect.cleandoc(cls.__doc__).split("\n")[0]

    derived = spec.model_copy(update=updates) if updates else spec
    cls.spec = derived
    return derived


def near_matches(wanted: str, known: Sequence[str], *, limit: int = 3) -> list[str]:
    """Ids close to *wanted*, for an error message that shortens the next step.

    A wrong node id is the most likely single failure when a model writes a node
    call, and it is almost always one character or one namespace away.
    """
    import difflib

    close = difflib.get_close_matches(wanted, list(known), n=limit, cutoff=0.6)
    if close:
        return close
    # Nothing lexically close: offer the namespace, which is the other common
    # miss — right category, invented name.
    namespace = wanted.partition(".")[0]
    return [k for k in known if k.startswith(f"{namespace}.")][:limit]
