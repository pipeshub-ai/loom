"""Shared mock HTTP client for reference workflow tests.

Replaces httpx.AsyncClient with a configurable mock that returns
canned responses based on URL patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock


@dataclass
class MockResponse:
    """Minimal httpx-compatible response."""

    status_code: int = 200
    _json: dict[str, Any] = field(default_factory=dict)
    text: str = ""

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise Exception(msg)


class MockHttpClient:
    """Async context manager that returns canned responses.

    Usage::

        client = MockHttpClient()
        client.add("POST", "openai.com", MockResponse(
            _json={"choices": [{"message": {"content": "hi"}}]}
        ))
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], MockResponse] = {}
        self._default = MockResponse(
            status_code=200,
            _json={},
        )

    def add(
        self,
        method: str,
        url_contains: str,
        response: MockResponse,
    ) -> None:
        """Register a canned response for requests matching URL."""
        self._routes[(method.upper(), url_contains)] = response

    async def _request(
        self,
        method: str,
        url: str,
        **_kwargs: Any,
    ) -> MockResponse:
        for (m, pattern), resp in self._routes.items():
            if m == method.upper() and pattern in url:
                return resp
        return self._default

    async def get(
        self, url: str, **kwargs: Any
    ) -> MockResponse:
        return await self._request("GET", url, **kwargs)

    async def post(
        self, url: str, **kwargs: Any
    ) -> MockResponse:
        return await self._request("POST", url, **kwargs)

    async def put(
        self, url: str, **kwargs: Any
    ) -> MockResponse:
        return await self._request("PUT", url, **kwargs)

    async def patch(
        self, url: str, **kwargs: Any
    ) -> MockResponse:
        return await self._request("PATCH", url, **kwargs)

    async def delete(
        self, url: str, **kwargs: Any
    ) -> MockResponse:
        return await self._request("DELETE", url, **kwargs)

    async def __aenter__(self) -> MockHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


def make_openai_response(content: str) -> MockResponse:
    """Build a canned OpenAI chat completion response."""
    return MockResponse(
        _json={
            "choices": [
                {"message": {"content": content}},
            ],
        },
    )


def make_openai_image_response(url: str) -> MockResponse:
    """Build a canned DALL-E image response."""
    return MockResponse(
        _json={"data": [{"url": url}]},
    )


def make_embedding_response(
    dim: int = 8, count: int = 1
) -> MockResponse:
    """Build a canned embedding response."""
    return MockResponse(
        _json={
            "data": [
                {"embedding": [0.1] * dim}
                for _ in range(count)
            ],
        },
    )


def make_mock_client() -> AsyncMock:
    """Create an AsyncMock that mimics httpx.AsyncClient."""
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock
