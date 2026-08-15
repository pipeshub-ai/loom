"""Structured output strategies.

Getting typed data out of a model is the single most failure-prone part of an agent, so
the strategy is explicit rather than implicit. Three modes, each with different reliability
and different model support:

* :attr:`OutputMode.NATIVE` — the provider's own structured-output/JSON-schema mode. Most
  reliable where supported; some models cannot combine it with tool calling.
* :attr:`OutputMode.TOOL` — the schema is offered as a special "final answer" tool. Works
  on virtually every tool-calling model, and composes with other tools.
* :attr:`OutputMode.PROMPTED` — the schema goes in the instructions and the reply is
  parsed. The universal fallback, and the least reliable.

Whatever the mode, validation failures are fed back to the model as text rather than
raised, because the next turn usually fixes them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter

from loom.core.exceptions import ModelRetry

FINAL_OUTPUT_TOOL = "final_output"


class OutputMode(StrEnum):
    AUTO = "auto"
    """Prefer native, fall back to a tool, then to prompting, based on the provider."""
    NATIVE = "native"
    TOOL = "tool"
    PROMPTED = "prompted"
    TEXT = "text"
    """No structure at all; the reply is returned verbatim."""


@dataclass
class OutputSpec:
    """How an agent's final answer should be shaped and validated."""

    type: Any = str
    mode: OutputMode = OutputMode.AUTO
    name: str = FINAL_OUTPUT_TOOL
    description: str = "Return the final answer in the required structure."
    max_retries: int = 2
    _adapter: TypeAdapter[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.type is not str and self.type is not None:
            self._adapter = TypeAdapter(self.type)

    @property
    def is_structured(self) -> bool:
        return self._adapter is not None

    def resolve_mode(self, *, supports_native: bool, has_tools: bool) -> OutputMode:
        if not self.is_structured:
            return OutputMode.TEXT
        if self.mode is not OutputMode.AUTO:
            return self.mode
        if supports_native and not has_tools:
            return OutputMode.NATIVE
        return OutputMode.TOOL

    def json_schema(self) -> dict[str, Any]:
        if self._adapter is None:
            return {"type": "string"}
        schema = self._adapter.json_schema()
        schema.setdefault("title", getattr(self.type, "__name__", "Output"))
        return schema

    def tool_schema(self) -> dict[str, Any]:
        """Wrap the output schema so it can be offered as a tool."""
        schema = self.json_schema()
        if schema.get("type") != "object":
            return {
                "type": "object",
                "properties": {"value": schema},
                "required": ["value"],
                "additionalProperties": False,
            }
        return schema

    def prompt_instructions(self) -> str:
        return (
            "Reply with a single JSON object and nothing else — no prose, no code fences. "
            "It must validate against this JSON Schema:\n"
            f"{json.dumps(self.json_schema(), indent=2)}"
        )

    # -- parsing ----------------------------------------------------------------------

    def parse(self, payload: Any) -> Any:
        """Validate a payload, raising :class:`ModelRetry` with actionable feedback."""
        if self._adapter is None:
            return payload if isinstance(payload, str) else json.dumps(payload)

        if isinstance(payload, dict) and set(payload) == {"value"}:
            candidate = payload["value"]
        else:
            candidate = payload

        try:
            return self._adapter.validate_python(candidate)
        except Exception as exc:
            raise ModelRetry(
                f"The response did not match the required schema: {exc}. "
                f"Reply again with JSON matching:\n{json.dumps(self.json_schema())}"
            ) from exc

    def parse_text(self, text: str) -> Any:
        """Parse a free-text reply, tolerating code fences and surrounding prose."""
        if self._adapter is None:
            return text
        extracted = extract_json(text)
        if extracted is None:
            raise ModelRetry(
                "No JSON object was found in the response. Reply with a single JSON "
                f"object matching:\n{json.dumps(self.json_schema())}"
            )
        return self.parse(extracted)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any | None:
    """Pull the first JSON value out of a model reply.

    Models wrap JSON in fences and apologies with some regularity; refusing to parse those
    turns a cosmetic problem into a failed run.
    """
    stripped = text.strip()
    for candidate in (stripped, *(m.group(1).strip() for m in _FENCE.finditer(text))):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                continue
    return None
