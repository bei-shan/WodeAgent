"""MCP JSON Schema is preserved from advertisement to dispatch."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools.mcp.adapter import MCPToolAdapter
from tools.mcp.schema_validation import MCPInputSchemaError, compile_mcp_input_schema
from tools.registry import ToolRegistry


class FakeClient:
    def __init__(self):
        self.calls = []

    def call_tool_sync(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return SimpleNamespace(content=[], isError=False, structuredContent={"ok": True})


NESTED = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2},
        "options": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                "mode": {"enum": ["fast", "safe"]},
            },
            "required": ["tags"],
            "additionalProperties": False,
        },
    },
    "required": ["query", "options"],
    "additionalProperties": False,
}


def make_registry(schema=NESTED):
    client = FakeClient()
    adapter = MCPToolAdapter(client, "remote_search", "search", schema=schema)
    registry = ToolRegistry()
    registry.register_tool(adapter)
    return client, adapter, registry


def test_full_nested_schema_is_advertised_unchanged():
    client, adapter, registry = make_registry()
    spec = next(x for x in registry.get_openai_tools() if x["function"]["name"] == "remote_search")
    assert spec["function"]["parameters"] == NESTED
    assert spec["function"]["parameters"] is not adapter._schema
    assert not client.calls


@pytest.mark.parametrize("arguments", [
    {},
    {"query": "ab", "options": {"tags": ["not-an-int"]}},
    {"query": "ab", "options": {"tags": []}},
    {"query": 2, "options": {"tags": [1]}},
    {"query": "ab", "options": {"tags": [1], "secret": "unexpected"}},
    {"query": "ab", "options": {"tags": [1], "mode": "invalid"}},
    {"query": "ab", "options": {"tags": [1]}, "extra": True},
])
def test_malformed_nested_inputs_never_call_remote(arguments):
    client, _, registry = make_registry()
    response = json.loads(registry.execute_tool("remote_search", arguments))
    assert response["status"] == "error"
    assert response["error"]["code"] == "MCP_PARAM_ERROR"
    assert not client.calls


def test_repeated_client_input_failures_do_not_trip_circuit_breaker():
    client, _, registry = make_registry()
    for _ in range(8):
        assert json.loads(registry.execute_tool("remote_search", {}))["status"] == "error"
    assert registry._circuit_breaker.is_available("remote_search")
    success = json.loads(registry.execute_tool(
        "remote_search", {"query": "ab", "options": {"tags": [1], "mode": "safe"}}
    ))
    assert success["status"] == "success"
    assert len(client.calls) == 1


def test_enum_and_extra_properties_do_not_echo_secret_values():
    client, _, registry = make_registry()
    secret = "SENSITIVE_OPENAI_SECRET_23466770"
    result = registry.execute_tool(
        "remote_search", {"query": "ab", "options": {"tags": [1], "mode": secret}}
    )
    response = json.loads(result)
    assert secret not in response["error"]["message"]
    assert "enum" in response["error"]["message"]
    assert not client.calls


def test_local_ref_and_array_constraints():
    schema = {
        "type": "object",
        "$defs": {"line": {"type": "object", "properties": {
            "value": {"type": "integer"}}, "required": ["value"]}},
        "properties": {"lines": {"type": "array", "items": {"$ref": "#/$defs/line"}}},
        "required": ["lines"],
    }
    client, _, registry = make_registry(schema)
    assert json.loads(registry.execute_tool("remote_search", {"lines": [{"value": 3}]}))["status"] == "success"
    assert json.loads(registry.execute_tool("remote_search", {"lines": [{"value": "3"}]}))["error"]["code"] == "MCP_PARAM_ERROR"
    assert len(client.calls) == 1


def test_external_refs_and_malformed_schemas_are_not_advertised_or_dispatched():
    for schema in (
        {"type": "object", "properties": {"value": {"$ref": "https://bad.test/schema.json"}}},
        {"type": "nonsense"},
        ["not-an-object"],
    ):
        client, adapter, registry = make_registry(schema)
        assert not any(x["function"]["name"] == "remote_search" for x in registry.get_openai_tools())
        response = json.loads(registry.execute_tool("remote_search", {"value": 1}))
        assert response["error"]["code"] == "MCP_PARAM_ERROR"
        assert not client.calls
        with pytest.raises(MCPInputSchemaError):
            adapter.get_input_schema()


def test_schema_size_and_complexity_are_bounded():
    with pytest.raises(MCPInputSchemaError):
        compile_mcp_input_schema({"type": "object", "description": "x" * 140_000})
    with pytest.raises(MCPInputSchemaError):
        compile_mcp_input_schema({"type": "object", "properties": {
            "many": {"$ref": "file:///etc/passwd"}
        }})
