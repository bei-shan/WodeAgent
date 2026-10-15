"""Session-local tool registration ownership and MCP reconnect atomicity."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.runtime.tool_catalog import ToolDiscoveryState, ToolSearchTool
from tools.base import Tool, ToolParameter
from tools.mcp.adapter import register_mcp_tools
from tools.registry import ToolRegistry


class Probe(Tool):
    strict_input_contract = True

    def __init__(self, name):
        super().__init__(name=name, description="Owned test adapter")
        self.calls = 0

    def get_parameters(self):
        return [ToolParameter(name="value", type="string", description="Value")]

    def run(self, parameters):
        self.calls += 1
        return json.dumps({"status": "success", "data": {}})


def test_registration_provenance_prevents_cross_source_replace_and_unregister():
    registry = ToolRegistry()
    builtin = Probe("Read")
    registry.register_tool(builtin, owner="builtin")
    assert registry.owner_of("Read") == "builtin"
    with pytest.raises(PermissionError, match="belongs"):
        registry.register_tool(Probe("Read"), owner="plugin:bad")
    with pytest.raises(PermissionError, match="belongs"):
        registry.unregister("Read")
    with pytest.raises(PermissionError, match="belongs"):
        registry.register_function("Read", "override", lambda _: "bad", owner="plugin:bad")
    assert registry.get_tool("Read") is builtin
    registry.unregister("Read", owner="builtin")
    assert registry.owner_of("Read") is None


def test_atomic_owner_refresh_drops_removed_names_without_affecting_other_sources():
    registry = ToolRegistry()
    a, b, other = Probe("mcp_files_a"), Probe("mcp_files_b"), Probe("mcp_other_x")
    registry.replace_owned_tools("mcp:files", [a, b])
    registry.register_tool(other, owner="mcp:other")
    old_step = registry.capture_tool_step()
    new = Probe("mcp_files_a")
    registry.replace_owned_tools("mcp:files", [new])
    assert registry.get_tool("mcp_files_a") is new
    assert registry.get_tool("mcp_files_b") is None
    assert registry.get_tool("mcp_other_x") is other
    result = json.loads(registry.execute_tool("mcp_files_a", {"value": "x"}, snapshot=old_step))
    assert result["error"]["code"] == "STALE_TOOL_SCHEMA"
    assert a.calls == b.calls == new.calls == 0


def test_owner_collision_is_atomic_and_preserves_previous_inventory():
    registry = ToolRegistry()
    first = Probe("files_first")
    builtin = Probe("Read")
    registry.replace_owned_tools("mcp:files", [first])
    registry.register_tool(builtin, owner="builtin")
    with pytest.raises(PermissionError, match="belongs"):
        registry.replace_owned_tools("mcp:files", [Probe("files_second"), Probe("Read")])
    assert registry.get_tool("files_first") is first
    assert registry.get_tool("files_second") is None
    assert registry.get_tool("Read") is builtin
    with pytest.raises(ValueError, match="duplicate"):
        registry.replace_owned_tools("mcp:files", [Probe("dup"), Probe("dup")])
    assert registry.get_tool("files_first") is first


def test_unregistration_and_clear_are_scoped_and_revoke_pending_discoveries():
    registry = ToolRegistry()
    remote = Probe("mcp_files_lookup")
    registry.replace_owned_tools("mcp:files", [remote])
    state = ToolDiscoveryState()
    registry.register_tool(ToolSearchTool(registry, state, can_promote=lambda _: True))
    registry.enable_deferred_discovery(state)
    registry.execute_tool("ToolSearch", {"query": "lookup"})
    assert "mcp_files_lookup" in state.pending_names()
    registry.unregister_owner("mcp:files")
    assert registry.get_tool("mcp_files_lookup") is None
    assert state.pending_names() == frozenset()
    assert registry.owner_of("mcp_files_lookup") is None
    with pytest.raises(PermissionError, match="ToolSearch"):
        registry.unregister_owner("legacy")


def test_mcp_reconnect_reuses_stable_names_and_evicts_stale_server_tools():
    registry = ToolRegistry()
    registry.register_tool(Probe("Read"), owner="builtin")
    items = [
        SimpleNamespace(name="read", description="MCP Read", inputSchema={
            "type": "object", "properties": {}, "additionalProperties": False,
        }),
        SimpleNamespace(name="list", description="MCP List", inputSchema={
            "type": "object", "properties": {}, "additionalProperties": False,
        }),
    ]
    client = SimpleNamespace(list_tools_sync=lambda: SimpleNamespace(tools=items))
    first = register_mcp_tools(registry, client, namespace="files")
    assert [item["name"] for item in first] == ["files_read", "files_list"]
    prev = registry.get_tool("files_read")
    items[:] = [items[0]]
    second = register_mcp_tools(registry, client, namespace="files")
    assert [item["name"] for item in second] == ["files_read"]
    assert registry.get_tool("files_read") is not prev
    assert registry.get_tool("files_list") is None
    assert "files_read_2" not in registry.list_tools()
    assert registry.owner_of("files_read") == "mcp:files"
    assert registry.owner_of("Read") == "builtin"


def test_mcp_sanitized_name_collision_cannot_take_another_server_adapter():
    registry = ToolRegistry()
    item = SimpleNamespace(name="get", description="Get", inputSchema={
        "type": "object", "properties": {}, "additionalProperties": False,
    })
    client = SimpleNamespace(list_tools_sync=lambda: SimpleNamespace(tools=[item]))
    first = register_mcp_tools(registry, client, namespace="one.two")
    first_adapter = registry.get_tool(first[0]["name"])
    second = register_mcp_tools(registry, client, namespace="one two")
    assert first[0]["name"] == "one_two_get"
    assert second[0]["name"] == "one_two_get_2"
    assert registry.get_tool("one_two_get") is first_adapter
    assert registry.owner_of("one_two_get") == "mcp:one.two"
    assert registry.owner_of("one_two_get_2") == "mcp:one two"


def test_mcp_cleanup_unregisters_only_mcp_names():
    from core.features.mcp import MCPFeature

    registry = ToolRegistry()
    builtin = Probe("Read")
    registry.register_tool(builtin, owner="builtin")
    registry.replace_owned_tools("mcp:files", [Probe("files_get")])
    registry.replace_owned_tools("mcp:search", [Probe("search_get")])
    agent = SimpleNamespace(tool_registry=registry, _mcp_clients=[])
    MCPFeature().cleanup(agent)
    assert registry.get_tool("Read") is builtin
    assert registry.get_tool("files_get") is None
    assert registry.get_tool("search_get") is None
