import json

import pytest

from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


class ArrayTool(Tool):
    def __init__(self):
        super().__init__(name="ArrayTool", description="array test")

    def get_parameters(self):
        return [
            ToolParameter(name="values", type="array", description="List of values", required=False),
        ]

    def run(self, parameters):  # pragma: no cover
        return "{}"


def test_array_parameter_schema_includes_items():
    registry = ToolRegistry()
    registry.register_tool(ArrayTool())

    tools = registry.get_openai_tools()
    schema = next(t for t in tools if t.get("function", {}).get("name") == "ArrayTool")
    values_schema = schema["function"]["parameters"]["properties"]["values"]

    assert values_schema["type"] == "array"
    assert "items" in values_schema


class SchemaProbe(Tool):
    def __init__(self, declaration):
        super().__init__(name="SchemaProbe", description="schema probe")
        self.declaration = declaration
        self.calls = 0

    def get_parameters(self):
        return self.declaration

    def run(self, parameters):
        self.calls += 1
        return self.create_success_response({}, "ok", parameters, 1)


@pytest.mark.parametrize("declaration", [
    None, "not-a-list", [object()],
    [ToolParameter(name="", type="string", description="invalid legacy name")],
    [ToolParameter(name="same", type="string", description="duplicate")] * 2,
])
def test_malformed_parameter_declaration_is_not_advertised_or_executable(declaration):
    parent = ToolRegistry()
    tool = SchemaProbe(declaration)
    parent.register_tool(tool)
    child = parent.derive_worker_registry()

    assert not parent.get_openai_tools()
    assert not child.get_openai_tools()
    assert child.get_tool("SchemaProbe") is None
    assert json.loads(parent.execute_tool("SchemaProbe", {}))["status"] == "error"
    assert json.loads(child.execute_tool("SchemaProbe", {}))["status"] == "error"
    assert tool.calls == 0


def test_legitimate_empty_parameter_list_remains_executable():
    parent = ToolRegistry()
    tool = SchemaProbe([])
    parent.register_tool(tool)
    child = parent.derive_worker_registry()

    assert child.get_openai_tools()[0]["function"]["parameters"]["properties"] == {}
    assert json.loads(child.execute_tool("SchemaProbe", {}))["status"] == "success"
    assert child.get_tool("SchemaProbe").calls == 1
    assert tool.calls == 0


@pytest.mark.parametrize("schema", [
    None, [], {"type": "array"}, {"type": "object", "properties": []},
    {"type": "object", "properties": {"value": {"type": "nonsense"}}},
    {"type": "object", "properties": {"x": {"$ref": "https://invalid.test/schema"}}},
    {"type": "object", "required": ["x", "x"]},
    {"type": "object", "description": "x" * 140_000},
    {"type": "object", "default": float("nan")},
    {"type": "object", "default": object()},
    {"type": "object", "properties": {1: {"type": "string"}}},
])
def test_malformed_input_schema_provider_is_not_bound(schema):
    class ProviderProbe(SchemaProbe):
        def get_input_schema(self):
            return schema

    parent = ToolRegistry()
    tool = ProviderProbe([])
    parent.register_tool(tool)
    child = parent.derive_worker_registry()
    assert not parent.get_openai_tools()
    assert not child.get_openai_tools()
    assert child.get_tool("SchemaProbe") is None
    assert json.loads(parent.execute_tool("SchemaProbe", {}))["status"] == "error"
    assert tool.calls == 0


def test_authoritative_provider_never_calls_incompatible_legacy_getter():
    class ProviderProbe(SchemaProbe):
        def get_parameters(self):
            raise AssertionError("Authoritative schema must be selected first")

        def get_input_schema(self):
            return {"type": "object", "properties": {"": {"type": ["string", "null"]}},
                    "required": [""], "additionalProperties": False}

    parent = ToolRegistry()
    tool = ProviderProbe(None)
    parent.register_tool(tool)
    child = parent.derive_worker_registry()
    for registry in (parent, child):
        snapshot = registry.capture_tool_step()
        assert len(snapshot.model_schemas()) == 1, "Authoritative provider bypasses legacy flattening"
        assert snapshot.model_schemas()[0]["function"]["parameters"] == tool.get_input_schema()
        assert json.loads(registry.execute_tool("SchemaProbe", {"": None}, snapshot=snapshot))["status"] == "success"
