"""Shared type aliases and small value helpers used across the SDK."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Generic, TypeAlias, TypeVar, get_args, overload

JSONValue: TypeAlias = Any
"""Anything that survives a round trip through the configured serializer."""

JSONDict: TypeAlias = dict[str, Any]

Duration: TypeAlias = "float | int | timedelta"
"""A span of time, expressed either in seconds or as a :class:`~datetime.timedelta`."""

T = TypeVar("T")
InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
DepsT = TypeVar("DepsT")

UNSET: Any = object()
"""Sentinel distinguishing "not provided" from an explicit ``None``."""


def to_seconds(duration: Duration) -> float:
    """Normalise a :data:`Duration` to a float number of seconds."""
    if isinstance(duration, timedelta):
        return duration.total_seconds()
    return float(duration)


# ---------------------------------------------------------------------------
# Result[T] — railway-oriented step outcome
# ---------------------------------------------------------------------------


class Result(Generic[T]):
    """Either a value or an error, for steps using ``on_error=OnError.ROUTE``.

    ``Result`` makes step failures values rather than exceptions, so downstream
    orchestration can branch on success/failure without ``try/except``.

    Usage::

        result = await ctx.step(risky_call, data)
        if isinstance(result, Failure):
            return await ctx.step(fallback, result)
        # result is the happy-path value
    """

    __slots__ = ("_error", "_value")

    def __init__(self, value: T | None = None, error: BaseException | None = None) -> None:
        self._value = value
        self._error = error

    @property
    def ok(self) -> bool:
        """``True`` when the step succeeded."""
        return self._error is None

    def unwrap(self) -> T:
        """Return the value, or raise the stored error."""
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]

    def unwrap_or(self, default: T) -> T:
        """Return the value on success, ``default`` on failure."""
        if self._error is not None:
            return default
        return self._value  # type: ignore[return-value]

    def unwrap_err(self) -> BaseException:
        """Return the stored error, or raise ``ValueError`` if this is a success."""
        if self._error is None:
            raise ValueError("Result is ok — no error to unwrap")
        return self._error

    @staticmethod
    def success(value: T) -> Result[T]:
        return Result(value=value)

    @staticmethod
    def failure(error: BaseException) -> Result[T]:
        return Result(error=error)

    @staticmethod
    def from_outcome(outcome: T | BaseException) -> Result[T]:
        """Wrap either a value or an exception into a ``Result``."""
        if isinstance(outcome, BaseException):
            return Result(error=outcome)
        return Result(value=outcome)

    def __repr__(self) -> str:
        if self.ok:
            return f"Result.success({self._value!r})"
        return f"Result.failure({self._error!r})"


# ---------------------------------------------------------------------------
# Batch[T] — typed collection from ctx.map
# ---------------------------------------------------------------------------


class Batch(Generic[T]):
    """Typed collection returned by ``ctx.map`` when ``return_exceptions=True``.

    Provides partition helpers so callers can separate successes from failures
    without manually iterating and type-checking each item.
    """

    __slots__ = ("_items",)

    def __init__(self, items: list[T | BaseException]) -> None:
        self._items = items

    def __iter__(self) -> Iterator[T | BaseException]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @overload
    def __getitem__(self, index: int) -> T | BaseException: ...

    @overload
    def __getitem__(self, index: slice) -> list[T | BaseException]: ...

    def __getitem__(self, index: int | slice) -> T | BaseException | list[T | BaseException]:
        return self._items[index]

    @property
    def successes(self) -> list[T]:
        """All items that are not exceptions."""
        return [item for item in self._items if not isinstance(item, BaseException)]

    @property
    def failures(self) -> list[BaseException]:
        """All items that are exceptions."""
        return [item for item in self._items if isinstance(item, BaseException)]

    @property
    def all_ok(self) -> bool:
        """``True`` if every item succeeded."""
        return not any(isinstance(item, BaseException) for item in self._items)

    def unwrap(self) -> list[T]:
        """Return all values if all succeeded, or raise the first failure."""
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
        return self._items  # type: ignore[return-value]

    def __repr__(self) -> str:
        ok = len(self.successes)
        fail = len(self.failures)
        return f"<Batch ok={ok} failed={fail} total={len(self._items)}>"


# ---------------------------------------------------------------------------
# Page[T] — paginated result
# ---------------------------------------------------------------------------


class Page(Generic[T]):
    """A single page of results from a paginated API call.

    Each page fetch is intended to be a durable sub-step when used inside
    a workflow, so mid-pagination crashes resume from the last completed page.

    Usage::

        page = await ctx.step(list_leads, cursor=None)
        all_leads = list(page.items)
        while page.has_more:
            page = await ctx.step(list_leads, cursor=page.cursor)
            all_leads.extend(page.items)
    """

    __slots__ = ("_cursor", "_has_more", "_items", "_total")

    def __init__(
        self,
        items: list[T],
        *,
        cursor: str | None = None,
        has_more: bool = False,
        total: int | None = None,
    ) -> None:
        self._items = items
        self._cursor = cursor
        self._has_more = has_more
        self._total = total

    @property
    def items(self) -> list[T]:
        """The items on this page."""
        return self._items

    @property
    def cursor(self) -> str | None:
        """Opaque cursor for fetching the next page."""
        return self._cursor

    @property
    def has_more(self) -> bool:
        """Whether more pages are available."""
        return self._has_more

    @property
    def total(self) -> int | None:
        """Total item count (if the API provides it)."""
        return self._total

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def collect(self, *pages: Page[T], max_items: int = 10_000) -> list[T]:
        """Collect items from this page and additional pages.

        Stops at *max_items* to prevent unbounded memory use.
        """
        result = list(self._items)
        for page in pages:
            result.extend(page.items)
            if len(result) >= max_items:
                return result[:max_items]
        return result

    def __repr__(self) -> str:
        more = ", has_more" if self._has_more else ""
        total = f", total={self._total}" if self._total is not None else ""
        return f"<Page items={len(self._items)}{more}{total}>"

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        """Teach Pydantic to journal and rehydrate a page.

        Without this a step returning ``Page`` cannot be serialized, so the
        result never survives replay — the paginated-fetch pattern in this
        class's own docstring only works because of this hook. The element
        type from ``Page[T]`` is threaded through, so ``Page[Lead]`` comes back
        holding ``Lead`` objects rather than bare dicts.
        """
        from pydantic_core import core_schema

        args = get_args(source_type)
        item_schema = handler.generate_schema(args[0]) if args else core_schema.any_schema()

        payload = core_schema.typed_dict_schema(
            {
                "items": core_schema.typed_dict_field(core_schema.list_schema(item_schema)),
                "cursor": core_schema.typed_dict_field(
                    core_schema.nullable_schema(core_schema.str_schema()), required=False
                ),
                "has_more": core_schema.typed_dict_field(
                    core_schema.bool_schema(), required=False
                ),
                "total": core_schema.typed_dict_field(
                    core_schema.nullable_schema(core_schema.int_schema()), required=False
                ),
            }
        )
        from_payload = core_schema.no_info_after_validator_function(
            lambda data: cls(
                items=data["items"],
                cursor=data.get("cursor"),
                has_more=data.get("has_more", False),
                total=data.get("total"),
            ),
            payload,
        )
        return core_schema.json_or_python_schema(
            json_schema=from_payload,
            python_schema=core_schema.union_schema(
                [core_schema.is_instance_schema(cls), from_payload]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda page: {
                    "items": page.items,
                    "cursor": page.cursor,
                    "has_more": page.has_more,
                    "total": page.total,
                },
                return_schema=payload,
            ),
        )
