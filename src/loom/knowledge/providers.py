"""Embedding providers, imported lazily.

Two, because two vendors publish an embedding endpoint LOOM already has an
extra for. Anthropic publishes none, which is why there is no
``AnthropicEmbeddings`` — inventing one that wrapped somebody else's model
would put a second vendor's dependency behind a first vendor's name.

    pip install loomsdk[openai]   # OpenAIEmbeddings
    pip install loomsdk[gemini]   # GeminiEmbeddings

Both record ``model_name``, and every index records the model that built it: two
models occupy two different spaces, and comparing across them is a plausible
number that means nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from loom.core.exceptions import ConfigurationError
from loom.knowledge.models import Vector

__all__ = ["GeminiEmbeddings", "OpenAIEmbeddings"]

#: Dimensions each default model produces, so a store can be sized before the
#: first call. Wrong here is caught by the store's model check rather than
#: silently mis-sizing an index.
_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "gemini-embedding-001": 3072,
    "text-embedding-004": 768,
}


class OpenAIEmbeddings:
    """OpenAI's embedding endpoint.

    ``dimensions`` is settable on the ``-3`` models: OpenAI truncates the
    vector server-side, which costs accuracy and saves storage. Shortening is a
    deliberate trade, so it is a constructor argument rather than a default.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        dimensions: int = 0,
    ) -> None:
        self.model_name = model
        self.dimensions = dimensions or _DIMENSIONS.get(model, 1536)
        self._requested = dimensions
        self._api_key = api_key
        self._base_url = base_url

    def _client(self) -> object:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError(
                "OpenAIEmbeddings needs the openai SDK: "
                "pip install 'loomsdk[openai]'"
            ) from exc
        kwargs = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return AsyncOpenAI(**kwargs)

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        payload: dict[str, object] = {"model": self.model_name, "input": list(texts)}
        if self._requested:
            payload["dimensions"] = self._requested
        response = await self._client().embeddings.create(**payload)  # type: ignore[attr-defined]
        # Sorted by index rather than trusted in order: OpenAI documents that
        # the array is ordered, and a mis-ordered batch would pair every chunk
        # with another one's meaning — invisibly.
        rows = sorted(response.data, key=lambda row: row.index)
        return [list(row.embedding) for row in rows]

    async def embed_query(self, text: str) -> Vector:
        """OpenAI does not distinguish a query from a document."""
        found = await self.embed_documents([text])
        return found[0] if found else []


class GeminiEmbeddings:
    """Google's embedding endpoint.

    Gemini **does** distinguish a query from a document, through
    ``task_type`` — ``RETRIEVAL_QUERY`` against ``RETRIEVAL_DOCUMENT``. Using
    one for both produces vectors that still compare and rank slightly worse,
    which is exactly the kind of silent degradation the two-method port exists
    to make expressible.
    """

    def __init__(
        self,
        model: str = "gemini-embedding-001",
        *,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model
        self.dimensions = _DIMENSIONS.get(model, 3072)
        self._api_key = api_key

    def _client(self) -> object:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError(
                "GeminiEmbeddings needs the google-genai SDK: "
                "pip install 'loomsdk[gemini]'"
            ) from exc
        return genai.Client(api_key=self._api_key) if self._api_key else genai.Client()

    async def _embed(self, texts: Sequence[str], task: str) -> list[Vector]:
        from google.genai import types

        response = await self._client().aio.models.embed_content(  # type: ignore[attr-defined]
            model=self.model_name,
            contents=list(texts),
            config=types.EmbedContentConfig(task_type=task),
        )
        return [list(row.values) for row in response.embeddings]

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> Vector:
        found = await self._embed([text], "RETRIEVAL_QUERY")
        return found[0] if found else []
