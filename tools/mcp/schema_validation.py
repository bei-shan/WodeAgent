"""Validate untrusted MCP input schemas without remote reference fetching.

MCP schemas are not equivalent to the flattened ToolParameter declarations.
Preserve nested schemas and use them both for model advertisement and for
last-mile validation before the remote call.
"""
from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class MCPInputSchemaError(ValueError):
    """The remote tool supplied an unusable/unsafe schema."""


MAX_SCHEMA_BYTES = 128 * 1024
MAX_SCHEMA_NODES = 4096
MAX_SCHEMA_DEPTH = 32


def compile_mcp_input_schema(schema: Any) -> Draft202012Validator:
    if not isinstance(schema, dict):
        raise MCPInputSchemaError("MCP input schema must be an object.")
    try:
        encoded = json.dumps(schema, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise MCPInputSchemaError("MCP input schema is not valid JSON.") from exc
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise MCPInputSchemaError("MCP input schema exceeds the size limit.")
    pending: list[tuple[Any, int]] = [(schema, 0)]
    seen = 0
    while pending:
        node, depth = pending.pop()
        seen += 1
        if seen > MAX_SCHEMA_NODES or depth > MAX_SCHEMA_DEPTH:
            raise MCPInputSchemaError("MCP input schema exceeds complexity limits.")
        if isinstance(node, dict):
            # JSON Schema's default resolution may retrieve external URLs.
            # Only references local to the same document are permitted.
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in node and (
                    not isinstance(node[key], str)
                    or (node[key] != "#" and not node[key].startswith("#/"))
                ):
                    raise MCPInputSchemaError("MCP schema cannot use external references.")
            pending.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            pending.extend((value, depth + 1) for value in node)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise MCPInputSchemaError("MCP input schema is invalid.") from exc
    # Keep the original schema as the resolver root so local #/$defs refs
    # remain correct. MCPToolAdapter already requires an arguments dict.
    return Draft202012Validator(schema)


def _safe_location(error: ValidationError) -> str:
    segments: list[str] = []
    for segment in list(error.absolute_path)[:8]:
        if isinstance(segment, int):
            segments.append(f"[{segment}]")
        else:
            label = re.sub(r"[^a-zA-Z0-9_-]", "_", str(segment))[:40]
            segments.append("." + (label or "field"))
    return "$" + "".join(segments)


def validate_mcp_input(validator: Draft202012Validator, arguments: dict[str, Any]) -> str | None:
    """Return bounded error metadata, never raw values or the server's error text."""
    error = next(validator.iter_errors(arguments), None)
    if error is None:
        return None
    # Validator.message can embed secrets, e.g. additionalProperties or enum
    # errors. Only report the failed keyword and structured location.
    keyword = str(error.validator or "schema")
    if keyword not in {
        "type", "required", "additionalProperties", "enum", "const",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
        "minProperties", "maxProperties", "uniqueItems", "anyOf", "oneOf",
        "allOf", "not", "contains", "dependentRequired", "dependentSchemas",
        "propertyNames", "multipleOf", "prefixItems", "items",
    }:
        keyword = "schema"
    return f"MCP arguments rejected at {_safe_location(error)} ({keyword})."
