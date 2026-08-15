"""Tool schema simplification for smaller models.

Reduces the complexity of JSON tool schemas so that models with
limited context windows and weaker JSON generation can still produce
valid tool calls.
"""

from __future__ import annotations

import copy
from typing import Any

# Common parameter names that should be kept even when stripping
# optional fields for the "small" tier.
_COMMON_FIELDS: frozenset[str] = frozenset(
    {"input", "name", "query", "text", "prompt", "content"},
)


class SchemaSimplifier:
    """Reduce tool-schema complexity based on model tier."""

    def simplify(
        self,
        schema: dict[str, Any],
        tier: str,
    ) -> dict[str, Any]:
        """Return a possibly simplified copy of *schema*.

        Args:
            schema: A JSON-Schema--style tool parameter schema.
            tier: One of ``"large"``, ``"medium"``, ``"small"``.

        Returns:
            The original schema (large) or a simplified copy.
        """
        if tier == "large":
            return schema

        result = copy.deepcopy(schema)
        props: dict[str, Any] = result.get("properties", {})

        # All non-large tiers: inline enum descriptions.
        for key, prop in props.items():
            self._inline_enum(key, prop)

        if tier == "small":
            result = self._strip_for_small(result)

        return result

    # ------------------------------------------------------------------
    # Medium + small: inline enum allowed values
    # ------------------------------------------------------------------

    @staticmethod
    def _inline_enum(key: str, prop: dict[str, Any]) -> None:
        """Append allowed values to the description of an enum."""
        enum = prop.get("enum")
        if not enum:
            return
        suffix = f"Allowed values: {', '.join(str(v) for v in enum)}"
        desc = prop.get("description", "")
        if suffix not in desc:
            prop["description"] = (
                f"{desc}. {suffix}" if desc else suffix
            )

    # ------------------------------------------------------------------
    # Small tier: flatten, strip optionals, add examples
    # ------------------------------------------------------------------

    def _strip_for_small(
        self,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        props: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        required_set = set(required)

        # Flatten small nested objects.
        for key in list(props):
            prop = props[key]
            if prop.get("type") == "object":
                inner = prop.get("properties", {})
                if inner and len(inner) <= 3:
                    props[key] = self._flatten_object(prop)

        # Drop optional params that are not "common".
        to_remove = [
            k
            for k in list(props)
            if k not in required_set and k not in _COMMON_FIELDS
        ]
        for k in to_remove:
            del props[k]

        # Add examples to remaining properties.
        for key, prop in props.items():
            if "examples" not in prop:
                prop["examples"] = self._generate_example(
                    key, prop,
                )

        return schema

    @staticmethod
    def _flatten_object(prop: dict[str, Any]) -> dict[str, Any]:
        """Convert a small nested object to a string with docs.

        Only applied when the nested object has at most three keys so
        the flattened description stays concise.
        """
        inner: dict[str, Any] = prop.get("properties", {})
        key_desc = ", ".join(
            f"{k}: {v.get('type', 'any')}" for k, v in inner.items()
        )
        desc = prop.get("description", "")
        note = f"JSON string with keys: {{{key_desc}}}"
        return {
            "type": "string",
            "description": f"{desc}. {note}" if desc else note,
        }

    @staticmethod
    def _generate_example(
        key: str,
        prop: dict[str, Any],
    ) -> list[Any]:
        """Produce example values based on the property type."""
        ptype = prop.get("type", "string")
        if ptype == "integer":
            return [1]
        if ptype == "number":
            return [1.0]
        if ptype == "boolean":
            return [True]
        if ptype == "array":
            return [[]]
        # Default to a string example.
        return [f"example_{key}"]
