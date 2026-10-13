"""Adapters for MCP tools to ToolRegistry."""

from __future__ import annotations

import time
from typing import Any

import logging
import re

from tools.base import Tool, ToolParameter, ErrorCode
from tools.mcp.protocol import to_protocol_result, to_protocol_error, to_protocol_invalid_param
from tools.mcp.schema_validation import MCPInputSchemaError, compile_mcp_input_schema, validate_mcp_input


class MCPToolAdapter(Tool):
    """Wrap an MCP tool and expose it as a local Tool."""

    def __init__(
        self,
        mcp_client,
        public_name: str,
        remote_name: str,
        tool_description: str | None = None,
        schema: dict | None = None,
    ):
        super().__init__(
            name=public_name,
            description=tool_description or "MCP tool",
            project_root=None,
            working_dir=None,
        )
        self._mcp_client = mcp_client
        self._remote_name = remote_name
        self._schema = schema if schema is not None else {"type": "object", "properties": {}}
        self._schema_error: MCPInputSchemaError | None = None
        try:
            self._validator = compile_mcp_input_schema(self._schema)
        except MCPInputSchemaError as exc:
            self._validator = None
            self._schema_error = exc

    def get_parameters(self) -> list[ToolParameter]:
        schema = self._schema if isinstance(self._schema, dict) else {}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return []
        required = set(schema.get("required") or [])
        parameters: list[ToolParameter] = []
        for name, spec in properties.items():
            if isinstance(spec, dict):
                type_name = spec.get("type") or "any"
                description = (spec.get("description") or "").strip()
                default = spec.get("default")
            else:
                type_name = "any"
                description = ""
                default = None
            parameters.append(
                ToolParameter(
                    name=name,
                    type=type_name,
                    description=description,
                    required=name in required,
                    default=default,
                )
            )
        return parameters

    def run(self, parameters: dict[str, Any]) -> str:
        start_time = time.monotonic()
        try:
            invalid = self._validate_params(parameters)
            if invalid:
                return to_protocol_invalid_param(invalid, parameters, self._remote_name, start_time)
        except Exception as exc:
            message = str(exc) or repr(exc)
            return to_protocol_error(
                message,
                parameters,
                self._remote_name,
                start_time,
                error_code=ErrorCode.MCP_PARSE_ERROR,
            )

        try:
            from core.runtime.cancellation import current_cancel_event
            cancel_event = current_cancel_event()
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("MCP request cancelled")
            result = self._mcp_client.call_tool_sync(self._remote_name, parameters)
        except InterruptedError:
            return to_protocol_error(
                "MCP tool call interrupted.",
                parameters, self._remote_name, start_time,
                error_code=ErrorCode.INTERRUPTED,
            )
        except TimeoutError as exc:
            message = str(exc) or "MCP tool call timeout"
            return to_protocol_error(
                message,
                parameters,
                self._remote_name,
                start_time,
                error_code=ErrorCode.MCP_TIMEOUT,
            )
        except ConnectionError as exc:
            message = str(exc) or "MCP server connection failed"
            return to_protocol_error(
                message,
                parameters,
                self._remote_name,
                start_time,
                error_code=ErrorCode.MCP_NETWORK_ERROR,
            )
        except Exception as exc:
            message = str(exc) or repr(exc)
            return to_protocol_error(
                message,
                parameters,
                self._remote_name,
                start_time,
                error_code=ErrorCode.MCP_EXECUTION_ERROR,
            )

        try:
            return to_protocol_result(result, parameters, self._remote_name, start_time)
        except Exception as exc:
            message = str(exc) or repr(exc)
            return to_protocol_error(
                message,
                parameters,
                self._remote_name,
                start_time,
                error_code=ErrorCode.MCP_PARSE_ERROR,
            )

    def get_input_schema(self) -> dict[str, Any]:
        """Authoritative JSON Schema for both model and runtime boundaries."""
        if self._schema_error is not None:
            raise self._schema_error
        # Copy so consumers cannot mutate the cached validated schema.
        from copy import deepcopy
        return deepcopy(self._schema)

    def _validate_params(self, parameters: dict[str, Any]) -> str:
        if self._validator is None:
            return "MCP input schema is unavailable or invalid."
        if not isinstance(parameters, dict):
            return "MCP arguments must be an object."
        return validate_mcp_input(self._validator, parameters) or ""


def register_mcp_tools(tool_registry, mcp_client, namespace: str | None = None) -> list[dict[str, object | None]]:
    """Discover tools from MCP server and register them to ToolRegistry."""
    logger = logging.getLogger(__name__)
    safe_name_pattern = re.compile(r"[^a-zA-Z0-9_-]")

    def sanitize_tool_name(name: str) -> str:
        sanitized = safe_name_pattern.sub("_", name)
        sanitized = re.sub(r"_+", "_", sanitized).strip("_")
        return sanitized or "tool"

    def ensure_unique(base_name: str) -> str:
        candidate = base_name
        counter = 2
        while tool_registry.get_tool(candidate) or tool_registry.get_function(candidate):
            candidate = f"{base_name}_{counter}"
            counter += 1
        return candidate

    tools = mcp_client.list_tools_sync()
    registered: list[dict[str, object | None]] = []
    for tool in tools.tools:
        remote_name = getattr(tool, "name", None) or getattr(tool, "tool_name", None)
        description = getattr(tool, "description", None)
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump()
        if not remote_name:
            continue
        raw_public_name = f"{namespace}:{remote_name}" if namespace else remote_name
        public_name = sanitize_tool_name(raw_public_name)
        public_name = ensure_unique(public_name)
        if public_name != raw_public_name:
            logger.info("Sanitized MCP tool name '%s' -> '%s'", raw_public_name, public_name)
        adapter = MCPToolAdapter(mcp_client, public_name, remote_name, description, schema)
        tool_registry.register_tool(adapter)
        registered.append({"name": public_name, "description": description, "schema": schema})
    return registered
