"""`Suspend`/`WorkflowCancelled`/`ContinueAsNew` derive from `BaseException`
specifically so `except Exception` in generated workflow code cannot swallow
them (see `core/exceptions.py`). A bare `except:` or an explicit
`except BaseException:` still can — this pins `CodeValidator` catching both
inside a `@workflow` body, and leaving `except Exception` (and code outside a
workflow body) alone.
"""
from __future__ import annotations

from loom.agents.validator import CodeValidator

_PREAMBLE = "from loom import workflow, step, Context\n\n"


def _validate(body: str) -> list:
    source = _PREAMBLE + (
        "@workflow\n"
        "async def my_flow(ctx: Context, input: dict) -> None:\n"
        f"{body}\n"
    )
    return CodeValidator().validate(source)


def _messages(issues: list) -> list[str]:
    return [i.message for i in issues if i.severity == "error"]


class TestBareExceptInWorkflowBody:
    def test_bare_except_around_a_suspending_call_is_flagged(self) -> None:
        issues = _validate(
            "    try:\n"
            "        await ctx.wait_for_approval('deploy')\n"
            "    except:\n"
            "        pass\n"
        )
        assert any("Bare 'except:'" in m for m in _messages(issues))

    def test_except_baseexception_around_a_suspending_call_is_flagged(self) -> None:
        issues = _validate(
            "    try:\n"
            "        await ctx.sleep(60)\n"
            "    except BaseException:\n"
            "        pass\n"
        )
        assert any("except BaseException" in m for m in _messages(issues))

    def test_baseexception_inside_a_tuple_of_caught_types_is_flagged(self) -> None:
        issues = _validate(
            "    try:\n"
            "        await ctx.wait_for_event('approved')\n"
            "    except (ValueError, BaseException):\n"
            "        pass\n"
        )
        assert any("except BaseException" in m for m in _messages(issues))

    def test_qualified_baseexception_reference_is_flagged(self) -> None:
        issues = _validate(
            "    import builtins\n"
            "    try:\n"
            "        await ctx.sleep(1)\n"
            "    except builtins.BaseException:\n"
            "        pass\n"
        )
        assert any("except BaseException" in m for m in _messages(issues))


class TestExceptExceptionIsLeftAlone:
    def test_except_exception_is_not_flagged(self) -> None:
        """`Suspend` et al. derive from `BaseException`, not `Exception` --
        this is the case the exception hierarchy already protects, so the
        validator must not also complain about it."""
        issues = _validate(
            "    try:\n"
            "        await ctx.step(risky)\n"
            "    except Exception:\n"
            "        pass\n"
        )
        assert not any(
            "BaseException" in m or "Bare 'except:'" in m for m in _messages(issues)
        )

    def test_except_a_specific_error_type_is_not_flagged(self) -> None:
        issues = _validate(
            "    try:\n"
            "        await ctx.step(risky)\n"
            "    except ValueError:\n"
            "        pass\n"
        )
        assert not any(
            "BaseException" in m or "Bare 'except:'" in m for m in _messages(issues)
        )


class TestOutsideWorkflowBody:
    def test_bare_except_in_a_step_function_is_not_flagged(self) -> None:
        """The check targets workflow bodies, where a swallowed Suspend
        strands a run -- a plain `@step` function raises no control signals
        of its own, so this is deliberately out of scope."""
        source = (
            _PREAMBLE
            + "@step\n"
            "async def risky() -> None:\n"
            "    try:\n"
            "        pass\n"
            "    except:\n"
            "        pass\n"
        )
        issues = CodeValidator().validate(source)
        assert not any("Bare 'except:'" in i.message for i in issues)
