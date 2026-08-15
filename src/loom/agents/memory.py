"""Conversation memory.

Sessions persist history across runs — the difference between a chatbot that remembers
yesterday and one that does not. The protocol is deliberately tiny so Redis, Postgres, or a
vector store can back it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Protocol, runtime_checkable

from loom.agents.messages import Message, Role


@runtime_checkable
class Session(Protocol):
    """Append-only conversation history keyed by a session id."""

    async def get(self, session_id: str, *, limit: int | None = None) -> list[Message]: ...

    async def append(self, session_id: str, messages: list[Message]) -> None: ...

    async def clear(self, session_id: str) -> None: ...


class InMemorySession:
    """Process-local history. Fine for tests and single-process chat."""

    def __init__(self, max_messages: int | None = None) -> None:
        self._history: dict[str, list[Message]] = defaultdict(list)
        self._max_messages = max_messages

    async def get(self, session_id: str, *, limit: int | None = None) -> list[Message]:
        history = self._history[session_id]
        window = limit or self._max_messages
        return list(history[-window:]) if window else list(history)

    async def append(self, session_id: str, messages: list[Message]) -> None:
        history = self._history[session_id]
        history.extend(messages)
        if self._max_messages and len(history) > self._max_messages:
            del history[: len(history) - self._max_messages]

    async def clear(self, session_id: str) -> None:
        self._history.pop(session_id, None)


class StoreBackedSession:
    """History persisted through any :class:`CacheStore`, so it survives restarts."""

    def __init__(
        self,
        store: Any,
        *,
        namespace: str = "session",
        ttl_seconds: float = 604800.0,
    ) -> None:
        self._store = store
        self._namespace = namespace
        self._ttl = ttl_seconds

    def _key(self, session_id: str) -> str:
        return f"{self._namespace}:{session_id}"

    async def get(self, session_id: str, *, limit: int | None = None) -> list[Message]:
        raw = await self._store.get(self._key(session_id))
        if not raw:
            return []
        messages = [Message.model_validate(item) for item in raw]
        return messages[-limit:] if limit else messages

    async def append(self, session_id: str, messages: list[Message]) -> None:
        existing = await self.get(session_id)
        combined = [*existing, *messages]
        await self._store.set(
            self._key(session_id), [m.model_dump(mode="json") for m in combined], self._ttl
        )

    async def clear(self, session_id: str) -> None:
        await self._store.delete(self._key(session_id))


async def replace_history(
    session: Session, session_id: str, messages: list[Message]
) -> None:
    """Overwrite a session's stored history with *messages*.

    The :class:`Session` protocol is append-only, which is the right shape for
    a transcript but the wrong one for "this run produced the whole updated
    conversation" — appending there would double every prior turn.
    """
    await session.clear(session_id)
    await session.append(session_id, messages)


def trim_history(
    messages: list[Message],
    *,
    max_messages: int = 40,
    keep_system: bool = True,
) -> list[Message]:
    """Drop the oldest turns while keeping system prompts and tool-call pairs intact.

    Splitting an assistant tool call from its result breaks most providers, so the window
    is snapped backwards to a safe boundary rather than cut at an exact count.
    """
    if len(messages) <= max_messages:
        return list(messages)

    system_messages = [m for m in messages if m.role is Role.SYSTEM] if keep_system else []
    body = [m for m in messages if m.role is not Role.SYSTEM]

    window = body[-max_messages:]
    # Never begin a window with an orphaned tool result.
    while window and window[0].role is Role.TOOL:
        window.pop(0)
    return [*system_messages, *window]
