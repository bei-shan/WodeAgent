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


@pytest.mark.parametrize("declaration", [None, "not-a-list", [object()]])
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
