"""Tests for Result[T] and Batch[T] types."""

from __future__ import annotations

import pytest

from loom.core.types import Batch, Result

# ---------------------------------------------------------------------------
# Result[T]
# ---------------------------------------------------------------------------


class TestResult:
    def test_success(self) -> None:
        r = Result.success(42)
        assert r.ok is True
        assert r.unwrap() == 42

    def test_failure(self) -> None:
        err = ValueError("boom")
        r: Result[int] = Result.failure(err)
        assert r.ok is False
        assert r.unwrap_err() is err

    def test_unwrap_raises_on_failure(self) -> None:
        r: Result[int] = Result.failure(ValueError("nope"))
        with pytest.raises(ValueError, match="nope"):
            r.unwrap()

    def test_unwrap_err_raises_on_success(self) -> None:
        r = Result.success(1)
        with pytest.raises(ValueError, match="no error"):
            r.unwrap_err()

    def test_unwrap_or(self) -> None:
        ok: Result[int] = Result.success(10)
        assert ok.unwrap_or(0) == 10

        fail: Result[int] = Result.failure(RuntimeError("x"))
        assert fail.unwrap_or(0) == 0

    def test_from_outcome_value(self) -> None:
        r = Result.from_outcome(99)
        assert r.ok
        assert r.unwrap() == 99

    def test_from_outcome_exception(self) -> None:
        err = TypeError("bad")
        r = Result.from_outcome(err)
        assert not r.ok
        assert r.unwrap_err() is err

    def test_repr_success(self) -> None:
        r = Result.success("hello")
        assert "success" in repr(r)
        assert "hello" in repr(r)

    def test_repr_failure(self) -> None:
        r: Result[str] = Result.failure(ValueError("x"))
        assert "failure" in repr(r)
        assert "ValueError" in repr(r)


# ---------------------------------------------------------------------------
# Batch[T]
# ---------------------------------------------------------------------------


class TestBatch:
    def test_basic_operations(self) -> None:
        b = Batch([1, 2, 3])
        assert len(b) == 3
        assert list(b) == [1, 2, 3]
        assert b[0] == 1
        assert b[1:3] == [2, 3]

    def test_all_ok(self) -> None:
        b = Batch([10, 20, 30])
        assert b.all_ok is True
        assert b.successes == [10, 20, 30]
        assert b.failures == []

    def test_mixed_results(self) -> None:
        err1 = ValueError("a")
        err2 = TypeError("b")
        b: Batch[int] = Batch([1, err1, 2, err2, 3])
        assert b.all_ok is False
        assert b.successes == [1, 2, 3]
        assert len(b.failures) == 2
        assert b.failures[0] is err1
        assert b.failures[1] is err2

    def test_unwrap_all_ok(self) -> None:
        b = Batch([1, 2, 3])
        assert b.unwrap() == [1, 2, 3]

    def test_unwrap_raises_first_failure(self) -> None:
        err = ValueError("first")
        b: Batch[int] = Batch([1, err, TypeError("second")])
        with pytest.raises(ValueError, match="first"):
            b.unwrap()

    def test_empty_batch(self) -> None:
        b: Batch[int] = Batch([])
        assert len(b) == 0
        assert b.all_ok is True
        assert b.successes == []
        assert b.failures == []
        assert b.unwrap() == []

    def test_repr(self) -> None:
        b: Batch[int] = Batch([1, ValueError("x"), 2])
        r = repr(b)
        assert "ok=2" in r
        assert "failed=1" in r
        assert "total=3" in r
