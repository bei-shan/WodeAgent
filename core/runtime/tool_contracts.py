"""Model-supplied tool argument checks at the registry boundary.

This is intentionally a small, strict subset of the published ToolParameter
schema: no coercion, no implicit defaults, no user values in diagnostics.
Interactive validation and deep domain rules remain with each tool.
"""
from __future__ import annotations

from typing import Any, Iterable


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _valid_type(value: Any, expected: str) -> bool:
    if expected in ("integer", "int"):
        return type(value) is int
    if expected in ("number", "float"):
        return type(value) in (int, float)
    if expected in ("boolean", "bool"):
        return type(value) is bool
    if expected in ("string", "str"):
        return isinstance(value, str)
    if expected in ("array", "list"):
        return isinstance(value, list)
    if expected in ("object", "dict"):
        return isinstance(value, dict)
    # Unsupported declarations may originate from plugins. Fail closed for
    # explicitly strict tools rather than silently skipping validation.
    return False


def validate_tool_input(parameters: dict[str, Any], definitions: Iterable[Any]) -> str | None:
    """Return a safe error string or None; never return supplied values."""
    specs = tuple(definitions)
    if not all(isinstance(getattr(x, "name", None), str) and getattr(x, "name") for x in specs):
        return "Invalid tool parameter declarations."
    names = {spec.name for spec in specs}
    if len(names) != len(specs):
        return "Duplicate tool parameter declarations."
    missing = sorted(
        p.name for p in specs
        if bool(getattr(p, "required", False)) and p.name not in parameters
    )
    if missing:
        return "Missing required parameter(s): " + ", ".join(missing[:5])
    extras = sorted(str(key) for key in parameters if key not in names)
    if extras:
        return "Unknown parameter(s): " + ", ".join(extras[:5])
    for spec in specs:
        if spec.name not in parameters:
            continue
        value = parameters[spec.name]
        if value is None and not bool(getattr(spec, "required", False)):
            continue
        expected = str(getattr(spec, "type", "string") or "string").lower().strip()
        if not _valid_type(value, expected):
            return f"Parameter '{spec.name}' expects {expected}, got {_type_name(value)}."
    return None

def error_envelope(code: str, message: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """A protocol-compliant tool error shared by hooks, parse and dispatch.

    Callers can serialize this payload directly; never include a traceback
    or raw tool output in the human-facing error message by default.
    """
    return {
        "status": "error",
        "data": {},
        "text": message,
        "error": {"code": code, "message": message},
        "stats": {"time_ms": 0},
        "context": {"cwd": ".", "params_input": parameters},
    }
