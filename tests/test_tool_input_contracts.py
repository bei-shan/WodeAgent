"""The registry is the final, fail-closed boundary for audited tools."""
from __future__ import annotations

import json
import logging

from core.runtime.tool_contracts import validate_tool_input
from core.runtime.tool_runtime import ToolRuntime
from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


class CounterTool(Tool):
    strict_input_contract = True

    def __init__(self, name="Strict"):
        super().__init__(name=name, description="Fake tool")
        self.runs = 0
        self.last_parameters = None

    def get_parameters(self):
        return [
            ToolParameter(name="value", type="integer", description="Counter", required=True),
            ToolParameter(name="enabled", type="boolean", description="Flag", required=False),
            ToolParameter(name="options", type="object", description="Options", required=False),
        ]

    def run(self, parameters):
        self.runs += 1
        self.last_parameters = dict(parameters)
        return json.dumps({"status": "success", "data": {"ok": True}})


def decode(result):
    return json.loads(result)


def test_schema_validation_is_pure_and_does_not_leak_values():
    tool = CounterTool()
    secret = "secret-value-must-not-echo"
    input_args = {"value": secret}
    reason = validate_tool_input(input_args, tool.get_parameters())
    assert "integer" in reason
    assert secret not in reason
    assert input_args == {"value": secret}


def test_missing_unknown_and_wrong_types_do_not_execute():
    tool = CounterTool()
    registry = ToolRegistry()
    registry.register_tool(tool)
    for args in ({}, {"value": True}, {"value": 1.5},
                 {"value": "5"}, {"value": 1, "extra": "ignore"}):
        result = decode(registry.execute_tool("Strict", args))
        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_PARAM"
        assert isinstance(result["error"]["message"], str)
        assert result["stats"]["time_ms"] == 0
    assert tool.runs == 0
    assert registry._circuit_breaker.is_available("Strict")
    assert registry.execute_tool("Strict", {"value": 2})


def test_optional_nulls_are_accepted_without_inventing_defaults():
    registry = ToolRegistry()
    tool = CounterTool()
    registry.register_tool(tool)
    result = decode(registry.execute_tool("Strict", {"value": 2, "enabled": None}))
    assert result["status"] == "success"
    assert tool.last_parameters == {"value": 2, "enabled": None}


def test_hook_modified_input_is_checked_after_interceptor():
    tool = CounterTool()
    registry = ToolRegistry()
    registry.register_tool(tool)
    class PoisonInput:
        name = "test_hook"
        def pre_tool_use(self, agent, name, params):
            return {"updated_input": {"value": "unsafe"}}
        def post_tool_use(self, agent, name, params, output):
            return []

    runtime = ToolRuntime(registry, logging.getLogger("test-contract"))
    result = decode(runtime.execute(
        "Strict", {"value": 3}, agent=object(), features=[PoisonInput()],
        emit_messages=lambda _: None, record_timing=lambda *_: None,
    ))
    assert result["error"]["code"] == "INVALID_PARAM"
    assert tool.runs == 0


def test_sandbox_denies_before_contract_even_when_input_invalid():
    registry = ToolRegistry()
    tool = CounterTool()
    registry.register_tool(tool)
    registry.restrict_to_tools({"Bash"})
    result = decode(registry.execute_tool("Strict", {"value": "bad"}))
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert tool.runs == 0


def test_unknown_schema_type_fails_closed_only_for_opted_in_tools():
    class BadSchema(CounterTool):
        def get_parameters(self):
            return [ToolParameter(name="value", type="exotic", description="Exotic")]
    registry = ToolRegistry()
    tool = BadSchema()
    registry.register_tool(tool)
    assert decode(registry.execute_tool("Strict", {"value": 1}))["error"]["code"] == "INVALID_PARAM"
    assert tool.runs == 0

    tool.strict_input_contract = False
    assert decode(registry.execute_tool("Strict", {"value": 1}))["status"] == "success"
    assert tool.runs == 1


def test_framework_lock_fields_checked_after_injection():
    class FakeWrite(CounterTool):
        def __init__(self):
            super().__init__(name="Write")

        def get_parameters(self):
            return [
                ToolParameter(name="path", type="string", description="Target"),
                ToolParameter(name="content", type="string", description="Text"),
                ToolParameter(name="expected_mtime_ms", type="integer", description="mtime", required=False),
                ToolParameter(name="expected_size_bytes", type="integer", description="size", required=False),
            ]

    registry = ToolRegistry()
    tool = FakeWrite()
    registry.register_tool(tool)
    registry._read_cache["hello.txt"] = {
        "path_resolved": "hello.txt", "file_mtime_ms": 123,
        "file_size_bytes": 8, "captured_at": 0.0,
    }
    result = decode(registry.execute_tool("Write", {"path": "hello.txt", "content": "new"}))
    assert result["status"] == "success"
    assert tool.last_parameters["expected_mtime_ms"] == 123
    assert tool.last_parameters["expected_size_bytes"] == 8
