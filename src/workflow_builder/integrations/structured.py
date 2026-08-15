"""Getting a typed answer out of a framework that returns prose.

Every adapter accepted an ``output_type`` and every one of them ignored it. The
signature promised a typed result and the body returned whatever the framework
said, so a caller asking for a model got a string — with no error, because a
string is a perfectly good ``Any``.

That is worse than not offering the parameter. `WorkflowCodingAgent` asks for a
``CodingOutput`` carrying code, an explanation, and the node plan; on an adapter
that drops it, the code is salvaged from a fenced block and the plan is *gone*,
silently, on a run that otherwise looks fine.

So the coercion lives here, once. An adapter passes whatever the framework
produced to :func:`coerce_output` and gets back either an instance of the
declared type or a clear failure. Frameworks that support native structured
output should still use it — this is the floor, not a replacement.

>>> from pydantic import BaseModel
>>> from workflow_builder.integrations.structured import coerce_output
>>> class Answer(BaseModel):
...     value: int
>>> coerce_output('{"value": 7}', Answer).value
7
>>> coerce_output({"value": 7}, Answer).value
7
"""

from __future__ import annotations

import json
import re
from typing import Any

from workflow_builder.core.exceptions import ValidationError

__all__ = ["coerce_output", "extract_json"]

#: A fenced block, optionally tagged ``json``. Models wrap structured answers in
#: one far more often than they return bare JSON, whatever the prompt asked.
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


def extract_json(text: str) -> Any | None:
    """The first JSON value in *text*, or ``None``.

    Three shapes, in the order they actually occur: a fenced block, the whole
    string, and a brace-delimited span inside prose. The last one exists
    because a model asked for JSON will happily prefix it with "Sure, here is
    the result:".
    """
    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _candidates(text: str) -> list[str]:
    stripped = text.strip()
    found = [block.strip() for block in _FENCE.findall(text)]
    found.append(stripped)
    # Widest brace span: a nested object would truncate on the first close.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if 0 <= start < end:
            found.append(stripped[start : end + 1])
    return found


def coerce_output(value: Any, output_type: type | None) -> Any:
    """Return *value* as *output_type*, or raise.

    ``None`` for *output_type* means the caller wants whatever came back, so
    the value passes through untouched — that is the common case and it costs
    nothing.

    Raises :class:`ValidationError` rather than returning the raw value on
    failure. Falling back silently is how the original bug worked: the caller
    believes it has a typed object, and finds out several attribute accesses
    later, somewhere unrelated.
    """
    if output_type is None:
        return value
    if isinstance(value, output_type):
        return value

    from pydantic import TypeAdapter

    payload: Any = value
    if isinstance(value, str):
        payload = extract_json(value)
        if payload is None:
            raise ValidationError(
                f"expected {output_type.__name__}, got text with no JSON in it. "
                "The adapter's framework returned prose — configure its native "
                "structured output, or have the agent reply with JSON."
            )

    # A framework may hand back its own wrapper around the answer.
    for attribute in ("output", "content", "result", "final_output"):
        if not isinstance(payload, dict | list) and hasattr(payload, attribute):
            payload = getattr(payload, attribute)
            break

    if isinstance(payload, str):
        payload = extract_json(payload) or payload

    try:
        return TypeAdapter(output_type).validate_python(payload)
    except Exception as exc:
        raise ValidationError(
            f"expected {output_type.__name__}, got {type(value).__name__} "
            f"that does not fit it: {exc}"
        ) from exc
