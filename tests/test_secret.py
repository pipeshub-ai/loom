"""``Secret`` must fail closed at the serialization boundary.

This is the test that would have caught the ``Credential``-is-a-``BaseModel``
leak: a plain string wrapped in ``Secret`` should be exactly as hard to
accidentally journal, log, or print as a raw token is easy.
"""

from __future__ import annotations

import pytest

from loom.core.exceptions import SerializationError
from loom.core.secret import Secret
from loom.core.serde import encode


def test_reveal_returns_the_wrapped_value() -> None:
    assert Secret("sk-live-abc123").reveal() == "sk-live-abc123"


@pytest.mark.parametrize("render", [repr, str])
def test_repr_and_str_never_contain_the_value(render) -> None:
    secret = Secret("sk-live-abc123")
    rendered = render(secret)
    assert "sk-live-abc123" not in rendered
    assert rendered == "Secret(***)"


def test_equality_compares_wrapped_values() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert Secret("a") != "a"  # bare-value comparisons are not supported


def test_hashable_by_wrapped_value() -> None:
    assert hash(Secret("a")) == hash(Secret("a"))
    assert len({Secret("a"), Secret("a"), Secret("b")}) == 2


def test_bool_reflects_the_wrapped_value_not_the_wrapper() -> None:
    assert bool(Secret("token")) is True
    assert bool(Secret("")) is False


class TestFailsClosedUnderSerde:
    """The property that matters: a step cannot accidentally journal one."""

    def test_encoding_a_bare_secret_raises(self) -> None:
        with pytest.raises(SerializationError):
            encode(Secret("sk-live-abc123"))

    def test_encoding_a_secret_nested_in_a_dict_raises(self) -> None:
        with pytest.raises(SerializationError):
            encode({"token": Secret("sk-live-abc123")})

    def test_encoding_a_secret_nested_in_a_list_raises(self) -> None:
        with pytest.raises(SerializationError):
            encode([1, Secret("sk-live-abc123")])

    def test_the_raw_value_never_appears_in_the_error_message(self) -> None:
        try:
            encode(Secret("sk-live-abc123-unique-marker"))
        except SerializationError as exc:
            assert "sk-live-abc123-unique-marker" not in str(exc)
        else:
            pytest.fail("expected SerializationError")
