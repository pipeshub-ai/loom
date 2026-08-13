"""Stand-in implementations for toolset operations, derived from their schemas.

The smoke sandbox has no credentials, so a workflow that talks to a real service
can only ever reach a 401 there — which proves nothing about the code and tempts
a repair loop into deleting the integration to make the error go away.

Substituting the toolset removes that problem, and the substitute is generated
rather than written: a manifest already declares each operation's
``output_schema``, so a value of the right shape can be built from it. Nobody
maintains a parallel set of fakes, and one that drifts from the real contract
cannot happen — there is only one contract.

``ToolsetManifest.fakes_module`` still wins where it is set. A generated stub
knows the shape of an answer, not its meaning, and some workflows need the
difference.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["fake_value", "install_fakes"]

#: Deterministic by construction. A fake that returned varying data would make
#: the replay check report a determinism fault in the harness, not the code.
_SCALARS: dict[str, Any] = {
    "string": "sample",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "null": None,
}


def fake_value(schema: dict[str, Any] | None, *, depth: int = 0) -> Any:
    """Build a value matching *schema*.

    Handles the JSON Schema subset manifests actually use: objects, arrays,
    scalars, enums, ``anyOf``, and the ``$ref``/``$defs`` pair Pydantic emits
    for a nested model.
    """
    if not isinstance(schema, dict) or depth > 6:
        return None

    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]

    if "$ref" in schema:
        return fake_value(_resolve(schema, schema["$ref"]), depth=depth + 1)

    for keyword in ("anyOf", "oneOf", "allOf"):
        options = schema.get(keyword)
        if isinstance(options, list) and options:
            # Prefer a shape over null: a caller reading .field off None learns
            # nothing except that the fake was unhelpful.
            chosen = next(
                (o for o in options if isinstance(o, dict) and o.get("type") != "null"),
                options[0],
            )
            merged = {**chosen}
            merged.setdefault("$defs", schema.get("$defs", {}))
            return fake_value(merged, depth=depth + 1)

    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), declared[0])

    if declared == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        built: dict[str, Any] = {}
        for name, sub in properties.items():
            if isinstance(sub, dict):
                sub = {**sub}
                sub.setdefault("$defs", schema.get("$defs", {}))
            built[name] = fake_value(sub, depth=depth + 1)
        return built

    if declared == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            items = {**items}
            items.setdefault("$defs", schema.get("$defs", {}))
        # One element: enough to exercise a loop, few enough to stay readable.
        return [fake_value(items, depth=depth + 1)]

    return _SCALARS.get(str(declared), "sample")


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any]:
    """Follow a local ``#/$defs/Name`` reference."""
    if not ref.startswith("#/"):
        return {}
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    if isinstance(node, dict):
        node.setdefault("$defs", root.get("$defs", {}))
        return node
    return {}


def install_fakes(manifest: Any) -> list[str]:
    """Replace a toolset's callables with stand-ins. Returns what was replaced.

    Prefers the manifest's own ``fakes_module`` when it declares one, and falls
    back to values built from each operation's ``output_schema``.
    """
    tools_module = getattr(manifest, "tools_module", "")
    if not tools_module:
        return []

    try:
        module = importlib.import_module(tools_module)
    except ImportError:
        return []

    overrides: dict[str, Any] = {}
    declared = getattr(manifest, "fakes_module", "")
    if declared:
        try:
            source = importlib.import_module(declared)
            overrides = {
                name: getattr(source, name)
                for name in dir(source)
                if not name.startswith("_") and callable(getattr(source, name))
            }
        except ImportError:
            overrides = {}

    replaced: list[str] = []
    for op in manifest.all_operations():
        if not op.function or not hasattr(module, op.function):
            continue

        if op.function in overrides:
            setattr(module, op.function, overrides[op.function])
            replaced.append(op.function)
            continue

        original = getattr(module, op.function)
        setattr(module, op.function, _stub(original, op))
        replaced.append(op.function)

    return replaced


def _coerce(payload: Any, output_type: Any) -> Any:
    """Shape *payload* into the step's declared return type.

    Through a ``TypeAdapter`` rather than calling the type: a step returning
    ``list[Model]`` is the common case, and a caller that reads ``.field`` off
    a plain dict fails with a message about dicts rather than about the fake.
    """
    if output_type is None or isinstance(output_type, str):
        return payload

    from pydantic import TypeAdapter

    try:
        return TypeAdapter(output_type).validate_python(payload)
    except Exception:
        return payload


def _stub(original: Any, op: Any) -> Any:
    """A callable of the same shape, returning schema-shaped data.

    Keeps the ``@step`` wrapper where there is one, so the substitute is still
    journalled and retried exactly as the real operation would be — the point is
    to remove the network, not the durability.
    """
    payload = fake_value(op.output_schema)
    coerced = _coerce(payload, getattr(original, "output_type", None))

    async def call(*_args: Any, **_kwargs: Any) -> Any:
        return coerced

    call.__name__ = op.function
    call.__doc__ = f"Fake {op.id}. Returns a value shaped by its output schema."

    inner = getattr(original, "fn", None)
    if inner is None:
        return call

    # A @step: rebuild it around the stub so retry and journalling still apply.
    from dataclasses import replace as replace_dataclass

    try:
        return replace_dataclass(original, fn=call)
    except Exception:
        return call
