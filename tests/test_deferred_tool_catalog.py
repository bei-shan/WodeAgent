"""Default-off, session-scoped discovery must match execution permission."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.codeAgent import CodeAgent
from core.context_engine.context_builder import ContextBuilder
from core.runtime.policy_middleware import DelegateModeMiddleware
from core.runtime.policy_middleware import PlanModeMiddleware
from core.runtime.tool_catalog import ToolDiscoveryState, ToolSearchTool
from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


class Probe(Tool):
    strict_input_contract = True

    def __init__(self, name, description="", usage_notes=""):
        super().__init__(name, description, usage_notes=usage_notes)
        self.calls = 0

    def get_parameters(self):
        return [ToolParameter(name="value", type="string", description="Value")]

    def run(self, parameters):
        self.calls += 1
        return json.dumps({"status": "success", "data": {"called": self.calls}})


def names(registry, **kwargs):
    return [x["function"]["name"] for x in registry.get_openai_tools(**kwargs)]


def make_registry(max_promoted=18, scope=None):
    registry = ToolRegistry()
    core = Probe("Read", "Read files")
    remote = Probe("mcp__files_lookup", "Find remote files", usage_notes="PRIVATE MCP USAGE TEXT")
    registry.register_tool(core)
    registry.register_tool(remote)
    scope = scope or (lambda _: True)
    state = ToolDiscoveryState(max_promoted=max_promoted)
    registry.register_tool(ToolSearchTool(registry, state, can_promote=scope))
    registry.enable_deferred_discovery(state, can_execute=scope)
    return registry, state, core, remote


def test_discovery_is_default_off_and_existing_schemas_still_visible():
    registry = ToolRegistry()
    registry.register_tool(Probe("Read", "Read files"))
    registry.register_tool(Probe("mcp__files_lookup", "Find remote files"))
    assert names(registry) == ["Read", "mcp__files_lookup"]


def test_hidden_tool_cannot_run_until_toolsearch_promotes_it():
    registry, state, core, remote = make_registry()
    assert names(registry) == ["Read", "ToolSearch"]
    assert "mcp__files_lookup" in names(registry, include_deferred=True)

    before = json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))
    assert before["error"]["code"] == "DISCOVERY_REQUIRED"
    assert remote.calls == 0

    result = json.loads(registry.execute_tool("ToolSearch", {"query": "files"}))
    assert result["status"] == "success"
    assert [match["name"] for match in result["data"]["matches"]] == ["mcp__files_lookup"]
    # A guess in the same model tool batch still has no executable schema.
    assert "mcp__files_lookup" not in names(registry)
    assert json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))["error"]["code"] == "DISCOVERY_REQUIRED"
    assert remote.calls == 0
    registry.activate_discovered_tools()  # next model turn / schema binding
    assert "mcp__files_lookup" in names(registry)
    assert json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))["status"] == "success"
    assert remote.calls == 1
    assert state.promoted_names() == frozenset({"mcp__files_lookup"})


def test_dynamic_policy_hides_and_blocks_previously_promoted_tools():
    allowed = {"mcp__files_lookup": True}
    def scope(name):
        return allowed.get(name, True)
    registry, state, _, remote = make_registry(scope=scope)
    assert json.loads(registry.execute_tool("ToolSearch", {"query": "lookup"}))["status"] == "success"
    registry.activate_discovered_tools()
    allowed["mcp__files_lookup"] = False
    assert "mcp__files_lookup" not in names(registry)
    denied = json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert remote.calls == 0
    assert json.loads(registry.execute_tool("ToolSearch", {"query": "lookup"}))["data"]["matches"] == []
    allowed["mcp__files_lookup"] = True
    assert "mcp__files_lookup" in names(registry)


def test_replacement_or_unregistration_revokes_discovered_executor():
    registry, state, _, original = make_registry()
    registry.execute_tool("ToolSearch", {"query": "lookup"})
    registry.activate_discovered_tools()
    replacement = Probe("mcp__files_lookup", "Different implementation")
    registry.register_tool(replacement)
    assert state.promoted_names() == frozenset()
    assert json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))["error"]["code"] == "DISCOVERY_REQUIRED"
    assert replacement.calls == original.calls == 0
    registry.execute_tool("ToolSearch", {"query": "lookup"})
    registry.activate_discovered_tools()
    assert "mcp__files_lookup" in names(registry)
    registry.unregister("mcp__files_lookup")
    assert state.promoted_names() == frozenset()


def test_query_limit_validation_and_promotion_budget():
    registry, state, _, _ = make_registry(max_promoted=1)
    registry.register_tool(Probe("mcp__files_search", "Search remote files"))
    for query, limit in [("", 2), ("files", 0), ("files", True), ("files", 7), ("x" * 129, 1)]:
        response = json.loads(registry.execute_tool("ToolSearch", {"query": query, "limit": limit}))
        assert response["error"]["code"] == "INVALID_PARAM"
    response = json.loads(registry.execute_tool("ToolSearch", {"query": "files", "limit": 6}))
    assert response["data"]["count"] == 1
    assert len(state.promoted_names()) == 0
    assert len(state.pending_names()) == 1
    registry.activate_discovered_tools()
    assert len(state.promoted_names()) == 1
    assert set(names(registry)) - {"Read", "ToolSearch"} == state.promoted_names()


def test_distinct_agent_registries_cannot_share_promotions():
    first, s1, _, _ = make_registry()
    second, s2, _, _ = make_registry()
    first.execute_tool("ToolSearch", {"query": "lookup"})
    first.activate_discovered_tools()
    assert "mcp__files_lookup" in names(first)
    assert "mcp__files_lookup" not in names(second)
    assert s2.promoted_names() == frozenset()


def test_prompt_usage_notes_are_hidden_until_promotion(tmp_path):
    registry, _, _, _ = make_registry()
    ctx = ContextBuilder(
        tool_registry=registry, project_root=str(tmp_path),
        system_prompt_override="Follow instructions. {tools}",
    )
    first = ctx.get_system_messages()[0]["content"]
    assert "PRIVATE MCP USAGE TEXT" not in first
    registry.execute_tool("ToolSearch", {"query": "lookup"})
    registry.activate_discovered_tools()
    second = ctx.get_system_messages()[0]["content"]
    assert "PRIVATE MCP USAGE TEXT" in second


def test_agent_setup_uses_same_mode_guard_for_discovery_and_direct_execution():
    registry = ToolRegistry()
    registry.register_tool(Probe("Read", "Read"))
    restricted = Probe("mcp__files_lookup", "Find remote files")
    registry.register_tool(restricted)
    agent = CodeAgent.__new__(CodeAgent)
    agent.config = SimpleNamespace(
        tool_deferred_discovery_enabled=True,
        tool_discovery_max_promoted=6,
    )
    agent.tool_registry = registry
    agent._sandbox_mode = "local_trusted"
    agent._in_plan_mode = False
    agent.delegate_mode = False
    agent.PLAN_MODE_TOOLS = PlanModeMiddleware.PLAN_MODE_TOOLS
    agent.DELEGATION_ALLOWED_TOOLS = DelegateModeMiddleware.DELEGATION_ALLOWED_TOOLS
    prompt = []
    agent.context_builder = SimpleNamespace(set_mcp_tools_prompt=prompt.append)
    agent._configure_tool_discovery()
    assert "ToolSearch" in names(registry)
    assert prompt
    registry.execute_tool("ToolSearch", {"query": "files"})
    # The model-facing Agent method explicitly commits pending schemas.
    assert "mcp__files_lookup" in {
        item["function"]["name"] for item in agent._get_openai_tools_for_current_mode()
    }

    agent._in_plan_mode = True
    assert "mcp__files_lookup" not in names(registry)
    denied = json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert restricted.calls == 0
    assert "ToolSearch" in names(registry)
    agent._in_plan_mode = False
    agent.delegate_mode = True
    assert "mcp__files_lookup" not in names(registry)
    agent.delegate_mode = False
    assert "mcp__files_lookup" in names(registry)


def test_discovery_control_tool_cannot_be_overridden_or_removed():
    registry, _, _, _ = make_registry()
    with pytest.raises(PermissionError, match="Cannot replace"):
        registry.register_tool(Probe("ToolSearch", "malicious override"))
    with pytest.raises(PermissionError, match="Cannot remove"):
        registry.unregister("ToolSearch")
    assert "ToolSearch" in names(registry)


def test_failed_authorization_callback_does_not_disclose_or_promote():
    def broken_scope(name):
        if name == "mcp__files_lookup":
            raise RuntimeError("backend auth provider unavailable")
        return True

    registry, state, _, remote = make_registry(scope=broken_scope)
    response = json.loads(registry.execute_tool("ToolSearch", {"query": "files"}))
    assert response["status"] == "success"
    assert response["data"]["matches"] == []
    assert state.promoted_names() == frozenset()
    denied = json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert remote.calls == 0


def test_pending_promotion_requires_next_schema_and_revocation_works():
    registry, state, _, remote = make_registry()
    registry.execute_tool("ToolSearch", {"query": "lookup"})
    assert state.promoted_names() == frozenset()
    assert state.pending_names() == frozenset({"mcp__files_lookup"})
    # Tool-call scheduling may dispatch additional calls in the same model
    # response; pending names are still denied at the registry boundary.
    denied = json.loads(registry.execute_tool("mcp__files_lookup", {"value": "x"}))
    assert denied["error"]["code"] == "DISCOVERY_REQUIRED"
    assert remote.calls == 0
    registry.unregister("mcp__files_lookup")
    assert state.pending_names() == frozenset()
    registry.activate_discovered_tools()
    assert state.promoted_names() == frozenset()
