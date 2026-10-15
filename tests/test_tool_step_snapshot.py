"""Execution may use only the schema+adapter actually sent to that model step."""
from __future__ import annotations

import json
from types import SimpleNamespace

from core.runtime.tool_catalog import ToolDiscoveryState, ToolSearchTool
from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


class Probe(Tool):
    strict_input_contract = True

    def __init__(self, name="Inspect"):
        super().__init__(name, "Read a named record")
        self.calls = []
        self.required = "value"

    def get_parameters(self):
        return [ToolParameter(name=self.required, type="string", required=True)]

    def run(self, parameters):
        self.calls.append(parameters)
        return json.dumps({"status": "success", "data": {"input": parameters}})


def response(registry, name, args, snapshot):
    return json.loads(registry.execute_tool(name, args, snapshot=snapshot))


def assert_stale(result):
    assert result["status"] == "error"
    assert result["error"]["code"] == "STALE_TOOL_SCHEMA"


def test_model_schemas_are_copied_and_registration_is_sealed():
    registry = ToolRegistry()
    original = Probe()
    registry.register_tool(original)
    sealed = registry.capture_tool_step()
    schema = sealed.model_schemas()
    assert [item["function"]["name"] for item in schema] == ["Inspect"]
    schema[0]["function"]["parameters"]["properties"].clear()
    assert sealed.model_schemas()[0]["function"]["parameters"]["properties"]["value"]
    assert response(registry, "Inspect", {"value": "a"}, sealed)["status"] == "success"
    replacement = Probe()
    registry.register_tool(replacement)
    assert_stale(response(registry, "Inspect", {"value": "b"}, sealed))
    assert replacement.calls == []
    next_step = registry.capture_tool_step()
    assert response(registry, "Inspect", {"value": "c"}, next_step)["status"] == "success"
    assert replacement.calls == [{"value": "c"}]
    assert original.calls == [{"value": "a"}]


def test_new_tool_and_deleted_tool_fail_closed_without_model_declaration():
    registry = ToolRegistry()
    before = registry.capture_tool_step()
    tool = Probe()
    registry.register_tool(tool)
    assert_stale(response(registry, "Inspect", {"value": "a"}, before))
    current = registry.capture_tool_step()
    registry.unregister("Inspect")
    assert_stale(response(registry, "Inspect", {"value": "a"}, current))
    assert tool.calls == []


def test_in_place_schema_and_description_changes_invalidate_snapshot():
    registry = ToolRegistry()
    tool = Probe()
    registry.register_tool(tool)
    old = registry.capture_tool_step()
    tool.description = "Different semantics"
    assert_stale(response(registry, "Inspect", {"value": "a"}, old))
    refreshed = registry.capture_tool_step()
    tool.required = "other"
    assert_stale(response(registry, "Inspect", {"other": "a"}, refreshed))
    assert tool.calls == []


def test_step_seals_are_registry_scoped_and_filter_is_fail_closed():
    first, second = ToolRegistry(), ToolRegistry()
    shared = Probe()
    first.register_tool(shared)
    second.register_tool(shared)
    first_step = first.capture_tool_step()
    assert_stale(response(second, "Inspect", {"value": "a"}, first_step))
    denied_step = first.capture_tool_step(allowed=lambda name: False)
    assert denied_step.model_schemas() == []
    assert_stale(response(first, "Inspect", {"value": "a"}, denied_step))
    failed_step = first.capture_tool_step(allowed=lambda name: 1 / 0)
    assert failed_step.model_schemas() == []
    assert shared.calls == []


def test_function_adapter_swap_cannot_hijack_old_llm_call():
    registry = ToolRegistry()
    called = []
    registry.register_function("lookup", "Original", lambda text: called.append(("old", text)) or "ok")
    step = registry.capture_tool_step()
    registry.register_function("lookup", "Replaced", lambda text: called.append(("new", text)) or "ok")
    assert_stale(response(registry, "lookup", {"input": "x"}, step))
    assert called == []
    next_step = registry.capture_tool_step()
    assert response(registry, "lookup", {"input": "y"}, next_step)["status"] == "success"
    assert called == [("new", "y")]


def test_deferred_search_does_not_enable_an_undisclosed_tool_in_same_step():
    registry = ToolRegistry()
    remote = Probe("mcp__lookup")
    registry.register_tool(Probe("Read"))
    registry.register_tool(remote)
    state = ToolDiscoveryState()
    registry.register_tool(ToolSearchTool(registry, state, can_promote=lambda _: True))
    registry.enable_deferred_discovery(state)
    step = registry.capture_tool_step()
    assert "mcp__lookup" not in [x["function"]["name"] for x in step.model_schemas()]
    search = response(registry, "ToolSearch", {"query": "lookup"}, step)
    assert search["status"] == "success"
    registry.activate_discovered_tools()
    # Even an externally forced activation cannot rewrite the prior step.
    assert_stale(response(registry, "mcp__lookup", {"value": "x"}, step))
    assert remote.calls == []
    next_step = registry.capture_tool_step()
    assert response(registry, "mcp__lookup", {"value": "x"}, next_step)["status"] == "success"


def test_mode_revocation_still_blocks_current_snapshot():
    allowed = {"Inspect": True}
    registry = ToolRegistry()
    tool = Probe()
    registry.register_tool(tool)
    state = ToolDiscoveryState()
    registry.register_tool(ToolSearchTool(registry, state, can_promote=lambda n: allowed.get(n, True)))
    registry.enable_deferred_discovery(state, can_execute=lambda n: allowed.get(n, True))
    step = registry.capture_tool_step()
    allowed["Inspect"] = False
    result = response(registry, "Inspect", {"value": "x"}, step)
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert tool.calls == []


def test_agent_exposes_exact_captured_schemas_when_registry_supports_snapshots():
    from agents.codeAgent import CodeAgent

    registry = ToolRegistry()
    registry.register_tool(Probe())
    agent = CodeAgent.__new__(CodeAgent)
    agent.tool_registry = registry
    agent._in_plan_mode = False
    agent.delegate_mode = False
    agent.PLAN_MODE_TOOLS = {"Read"}
    schemas = agent._get_openai_tools_for_current_mode()
    assert len(schemas) == 1
    assert agent._model_tool_step_snapshot.model_schemas() == schemas
    registry.register_tool(Probe())
    assert_stale(response(registry, "Inspect", {"value": "a"}, agent._model_tool_step_snapshot))
