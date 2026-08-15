"""One gate between a payload and a run.

A workflow publishes ``input_schema()`` so a caller can get the shape right
first time. Checking it was, until now, something only the MCP boundary did —
so the CLI, the HTTP surface, the trigger dispatcher, and the queue consumer
each created a run record and then discovered the mismatch from inside a step
body, several operations in, as an ``AttributeError``. That reads as a broken
workflow rather than a wrong input, and it leaves a failed run in history for
work that could never have happened.

The check is deliberately shallow: the declared top-level type, and the
required properties of an object. Anything deeper is the workflow's own model's
job, and it will say it better — a Pydantic error names the field, the expected
type, and the value it got. What this catches is the class of mistake that
*cannot* produce a good error later, because by the time it surfaces the
context is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Mismatch", "shape_error"]

#: JSON Schema type names mapped to what Python accepts for them.
JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

#: A minimal literal per type, so an error can show rather than describe.
EXAMPLES: dict[str, str] = {
    "object": '{"field": ...}',
    "array": "[...]",
    "string": '"text"',
    "integer": "1",
    "number": "1.5",
    "boolean": "true",
    "null": "null",
}


@dataclass(frozen=True)
class Mismatch:
    """Why a payload cannot start this workflow."""

    message: str
    schema: dict[str, Any]
    example: str = "null"
    path: str = ""
    """Dotted location of the offending field, when one field is at fault."""

    def as_payload(self) -> dict[str, Any]:
        """The shape a model-facing boundary returns instead of raising."""
        return {
            "error": self.message,
            "expected_schema": self.schema,
            "example_input_json": self.example,
        }


def shape_error(schema: Any, payload: Any) -> Mismatch | None:
    """Return why *payload* cannot start a workflow declaring *schema*.

    ``None`` means "nothing here can be ruled out", which is not the same as
    "valid" — an undeclared or unrepresentable schema returns ``None`` because
    the workflow said nothing this can check.

    Args:
        schema: The workflow's ``input_schema()``, or ``None``.
        payload: The value a caller wants to run with.
    """
    if not isinstance(schema, dict):
        return None

    if payload is None:
        # ``Runtime.run(target)`` defaults its input to None, so None is
        # indistinguishable from "not supplied". Rejecting the value the API
        # itself defaults to would refuse every workflow whose input is
        # optional in practice — including the common one that declares a
        # parameter and ignores it. Absence is a different mistake from a wrong
        # shape, and the body is where it reads best.
        return None

    declared = schema.get("type")
    if not isinstance(declared, str) or declared not in JSON_TYPES:
        return None

    accepted = JSON_TYPES[declared]
    # ``bool`` is a subclass of ``int``, so True would otherwise satisfy a
    # declared integer — the one place Python's numeric tower lies about intent.
    wrong_type = not isinstance(payload, accepted) or (
        declared in {"integer", "number"} and isinstance(payload, bool)
    )
    if wrong_type:
        return Mismatch(
            message=(
                f"This workflow takes {declared}, but the input was "
                f"{type(payload).__name__}. Nothing was run."
            ),
            schema=schema,
            example=EXAMPLES.get(declared, "null"),
        )

    if declared == "object" and isinstance(payload, dict):
        missing = _missing_required(schema, payload)
        if missing:
            listed = ", ".join(repr(name) for name in missing)
            return Mismatch(
                message=(
                    f"This workflow's input is missing required "
                    f"{'field' if len(missing) == 1 else 'fields'} {listed}. "
                    f"Nothing was run."
                ),
                schema=schema,
                example=EXAMPLES["object"],
                path=missing[0],
            )

    return None


def _missing_required(schema: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Required top-level properties absent from *payload*.

    Only the top level. A nested object's requirements are enforced by whatever
    model declared them, whose error message is better than anything that could
    be reconstructed from the schema here.
    """
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [name for name in required if isinstance(name, str) and name not in payload]
