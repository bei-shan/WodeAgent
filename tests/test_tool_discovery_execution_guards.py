"""Regression checks: model tool visibility is not execution authorization."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from agents.codeAgent import CodeAgent
from core.runtime.policy_middleware import PlanModeMiddleware
from core.runtime.tool_runtime import ToolRuntime
from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


class _ProbeTool(Tool):
    def __init__(self, name="Read", *, broken_schema=False):
        super().__init__(name=name, description="Probe")
        self.calls = 0
        self.broken_schema = broken_schema

    def get_parameters(self):
        if self.broken_schema:
            raise RuntimeError("schema failed")
        return [ToolParameter(name="path", type="string", description="Target")]

    def run(self, params):
        self.calls += 1
        return json.dumps({"status": "success", "data": {"called": True}})


def _run(runtime, agent, feature, tool, params):
    return json.loads(runtime.execute(
        tool, params, agent=agent, features=[feature],
        emit_messages=lambda _: None, record_timing=lambda *_: None,
    ))


def test_plan_mode_not_just_schema_filter_but_hard_execution_guard():
    registry = ToolRegistry()
    read = _ProbeTool("Read")
    bash = _ProbeTool("Bash")
    registry.register_tool(read)
    registry.register_tool(bash)
    feature = PlanModeMiddleware()
    agent = SimpleNamespace()
    feature.init(agent)
    agent._in_plan_mode = True

    runtime = ToolRuntime(registry, logging.getLogger("plan-execution-guard"))
    denied = _run(runtime, agent, feature, "Bash", {"path": "."})
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert bash.calls == 0

    allowed = _run(runtime, agent, feature, "Read", {"path": "."})
    assert allowed["status"] == "success"
    assert read.calls == 1

    # The same runtime must honor mode changes; cached decisions cannot
    # authorize writes when the Agent enters plan mode later.
    agent._in_plan_mode = False
    assert _run(runtime, agent, feature, "Bash", {"path": "."})["status"] == "success"
    assert bash.calls == 1
    agent._in_plan_mode = True
    assert _run(runtime, agent, feature, "Bash", {"path": "."})["status"] == "error"
    assert bash.calls == 1


def test_plan_mode_tool_discovery_matches_runtime_policy():
    registry = ToolRegistry()
    registry.register_tool(_ProbeTool("Read"))
    registry.register_tool(_ProbeTool("Bash"))
    agent = CodeAgent.__new__(CodeAgent)
    agent.tool_registry = registry
    agent._in_plan_mode = True
    agent.delegate_mode = False
    agent.PLAN_MODE_TOOLS = PlanModeMiddleware.PLAN_MODE_TOOLS
    names = {t["function"]["name"] for t in agent._get_openai_tools_for_current_mode()}
    assert names == {"Read"}


@pytest.mark.parametrize("tool_first", [True, False])
def test_object_and_function_tool_names_cannot_collide(tool_first):
    registry = ToolRegistry()
    object_tool = _ProbeTool("Shared")
    function = lambda value: json.dumps({"status": "success", "data": {"value": value}})
    if tool_first:
        registry.register_tool(object_tool)
        with pytest.raises(ValueError, match="already registered"):
            registry.register_function("Shared", "Function", function)
    else:
        registry.register_function("Shared", "Function", function)
        with pytest.raises(ValueError, match="already registered"):
            registry.register_tool(object_tool)
    assert [t["function"]["name"] for t in registry.get_openai_tools()] == ["Shared"]


def test_function_tool_schema_and_dispatch_accept_only_one_string_input():
    registry = ToolRegistry()
    seen = []
    def function(value: str):
        seen.append(value)
        return json.dumps({"status": "success", "data": {"value": value}})
    registry.register_function("Lookup", "lookup a name", function)
    schema = registry.get_openai_tools()[0]["function"]["parameters"]
    assert schema["required"] == ["input"]
    assert schema["additionalProperties"] is False

    assert json.loads(registry.execute_tool("Lookup", {"input": "first"}))["status"] == "success"
    assert json.loads(registry.execute_tool("Lookup", "second"))["status"] == "success"
    assert seen == ["first", "second"]

    for invalid in ({}, {"input": 4}, {"input": "ok", "extra": "x"}, 123, None):
        response = json.loads(registry.execute_tool("Lookup", invalid))
        assert response["error"]["code"] == "INVALID_PARAM"
    assert seen == ["first", "second"]
    assert registry._circuit_breaker.is_available("Lookup")


def test_broken_parameter_declarations_are_not_advertised_as_empty_schema():
    registry = ToolRegistry()
    registry.register_tool(_ProbeTool("Broken", broken_schema=True))
    registry.register_tool(_ProbeTool("Read"))
    assert [t["function"]["name"] for t in registry.get_openai_tools()] == ["Read"]
