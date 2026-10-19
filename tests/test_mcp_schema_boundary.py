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


def test_valid_json_schema_boolean_property_and_unlisted_required_field_remain_available():
    schema = {
        "type": "object", "properties": {"blocked": False},
        "required": ["value"], "additionalProperties": True,
    }
    client, _, registry = make_registry(schema)
    assert registry.get_openai_tools()[0]["function"]["parameters"] == schema
    assert json.loads(registry.execute_tool("remote_search", {"value": "ok"}))["status"] == "success"
    assert client.calls == [("search", {"value": "ok"})]


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


@pytest.mark.parametrize("schema,valid_inputs,invalid_input", [
    pytest.param({
        "type": "object", "properties": {"value": {"type": ["string", "null"]}},
        "required": ["value"], "additionalProperties": False,
    }, [{"value": "string"}, {"value": None}], {"value": 7}, id="nullable-type-array"),
    pytest.param({
        "type": "object", "properties": {"": {"type": "string"}},
        "required": [""], "additionalProperties": False,
    }, [{"": "empty-name"}], {"": 7}, id="empty-property-name"),
    pytest.param({
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    }, [{"value": "ordinary"}], {"value": 7}, id="ordinary-string"),
    pytest.param({
        "type": "object", "properties": {"blocked": False, "anything": True},
        "required": ["value"], "additionalProperties": True,
    }, [{"value": 7, "anything": None}], {"value": 7, "blocked": None}, id="boolean-and-unlisted-required"),
    pytest.param({
        "type": "object", "$defs": {"item": {"type": "integer"}},
        "properties": {"values": {"type": "array", "items": {"$ref": "#/$defs/item"}}},
        "required": ["values"], "additionalProperties": False,
    }, [{"values": [7]}], {"values": ["7"]}, id="nested-local-ref"),
    pytest.param({
        "type": "object", "properties": {}, "additionalProperties": False,
    }, [{}], {"extra": True}, id="zero-arguments"),
])
def test_parent_and_worker_preserve_authoritative_schema_and_dispatch_snapshots(schema, valid_inputs, invalid_input):
    client, adapter, parent = make_registry(schema)
    child = parent.derive_worker_registry()
    for registry in (parent, child):
        snapshot = registry.capture_tool_step()
        advertised = registry.get_openai_tools()
        assert len(advertised) == 1, "Legal authoritative schema must remain available"
        assert advertised[0]["function"]["parameters"] == schema
        assert snapshot.model_schemas()[0]["function"]["parameters"] == schema
        assert snapshot.model_schemas()[0]["function"]["parameters"] is not adapter._schema
        for arguments in valid_inputs:
            previous_calls = len(client.calls)
            result = json.loads(registry.execute_tool("remote_search", arguments, snapshot=snapshot))
            assert result["status"] == "success"
            assert len(client.calls) == previous_calls + 1
            assert client.calls[-1] == ("search", arguments)
        before_invalid = list(client.calls)
        result = json.loads(registry.execute_tool("remote_search", invalid_input, snapshot=snapshot))
        assert result["error"]["code"] == "MCP_PARAM_ERROR"
        assert client.calls == before_invalid
    assert child.get_tool("remote_search")._mcp_client is adapter._mcp_client is client
    assert client.calls == [("search", arguments) for _ in range(2) for arguments in valid_inputs]
