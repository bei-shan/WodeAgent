"""Workers inherit bounded capabilities without mutating the parent session."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.memory.contracts import MemoryScope
from core.runtime.tool_authorization import HostedToolAuthority
from core.runtime.tool_catalog import ToolDiscoveryState, ToolSearchTool
from core.team_engine.execution import ExecutionService
from core.team_engine.turn_executor import TurnExecutor
from tools.base import Tool, ToolParameter
from tools.builtin.enter_plan_mode import EnterPlanModeTool
from tools.builtin.enter_worktree import EnterWorktreeTool
from tools.builtin.exit_plan_mode import ExitPlanModeTool
from tools.builtin.exit_worktree import ExitWorktreeTool
from tools.builtin.send_message import SendMessageTool
from tools.builtin.task import TaskTool
from tools.builtin.team_approve_plan import TeamApprovePlanTool
from tools.builtin.team_approvals import TeamApprovalsTool
from tools.builtin.team_cleanup import TeamCleanupTool
from tools.builtin.team_create import TeamCreateTool
from tools.builtin.team_delete import TeamDeleteTool
from tools.builtin.team_fanout import TeamFanoutTool
from tools.builtin.team_retry import TeamRetryTool
from tools.builtin.team_status import TeamStatusTool
from tools.builtin.team_task_create import TeamTaskCreateTool
from tools.builtin.team_task_list import TeamTaskListTool
from tools.mcp.adapter import MCPToolAdapter
from tools.permission_gate import PermissionGate
from tools.registry import ToolRegistry


class MarkerTool(Tool):
    strict_input_contract = True

    def __init__(self, name, *, gate=None, service=None):
        super().__init__(name, "Marker", permission_gate=gate)
        self.calls = []
        self.service = service
        self.field = "value"

    def get_parameters(self):
        return [ToolParameter(name=self.field, type="string", description="Marker", required=True)]

    def run(self, parameters):
        self.calls.append(parameters)
        return self.create_success_response({"echo": parameters[self.field]}, "ok", parameters, 1)


def _team_registry(parent, tmp_path, policy=None):
    member = {"name": "worker", "role": "developer", "tool_policy": policy or {}}
    service = ExecutionService(
        project_root=str(tmp_path), llm=Mock(), tool_registry=parent,
        work_executor=None,
        read_team_fn=lambda _: {"members": [member]},
    )
    return service._build_teammate_registry("team", "worker")[0]


def _task_registry(parent, tmp_path):
    return TaskTool(project_root=tmp_path, main_llm=Mock(), tool_registry=parent)._create_filtered_registry()


def _response(name, arguments):
    return {"choices": [{"message": {"content": "", "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}}]}


def _observation(turn):
    return json.loads(next(m["content"] for m in turn["messages"] if m["role"] == "tool"))


@pytest.mark.parametrize("derive,name", [(_team_registry, "Write"), (_task_registry, "Read")])
def test_worker_gate_is_independent_and_parent_broker_survives(derive, name, tmp_path):
    parent = ToolRegistry()
    gate = PermissionGate(tmp_path, interactive=True)
    broker = Mock(return_value="granted")
    gate._broker = broker
    original = MarkerTool(name, gate=gate)
    parent.register_tool(original)

    child = derive(parent, tmp_path)
    bound = child.get_tool(name)
    assert bound is not original
    assert bound._permission_gate is not gate
    assert bound._permission_gate._interactive is False
    assert bound._permission_gate._cache is gate._cache
    assert bound._permission_gate._broker is None
    assert parent.get_tool(name) is original
    assert original._permission_gate is gate
    assert gate._broker is broker
    assert bound._permission_gate.ask(str(tmp_path.parent / "outside"), name, "read") == "denied"
    assert broker.call_count == 0
    assert json.loads(child.execute_tool(name, {"value": "child"}))["status"] == "success"
    assert parent.get_tool(name) is original and original._permission_gate is gate
    assert gate.ask(str(tmp_path.parent / "outside"), name, "read") == "granted"
    broker.assert_called_once()


def test_team_worker_omits_parent_only_controls_even_when_explicitly_allowed(tmp_path):
    parent = ToolRegistry()
    controls = ("Task", "AskUser", "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree", "ToolSearch")
    for name in (*controls, "Read"):
        parent.register_tool(MarkerTool(name))
    interaction = object()
    parent.get_tool("AskUser")._input_func = interaction
    child = _team_registry(parent, tmp_path, {"allowlist": [*controls, "Read"]})
    assert child.get_tool("Read") is not None
    for name in controls:
        assert child.get_tool(name) is None
        assert name not in {s["function"]["name"] for s in child.get_openai_tools()}
    assert parent.get_tool("AskUser")._input_func is interaction


@pytest.mark.parametrize("explicit_allow", [False, True])
def test_default_team_worker_cannot_dispatch_parent_team_controls(tmp_path, explicit_allow):
    manager = Mock()
    manager.get_status.return_value = {"team_name": "demo", "message_counts": {}}
    parent = ToolRegistry()
    controls = (
        TeamApprovePlanTool, TeamCreateTool, TeamDeleteTool,
        TeamCleanupTool, TeamFanoutTool, TeamRetryTool, SendMessageTool,
    )
    for control in controls:
        parent.register_tool(control(project_root=tmp_path, team_manager=manager))
    parent.register_tool(TeamStatusTool(project_root=tmp_path, team_manager=manager))
    parent.register_tool(TeamApprovalsTool(project_root=tmp_path, team_manager=manager))
    parent.register_tool(TeamTaskListTool(project_root=tmp_path, team_manager=manager))
    parent.register_tool(TeamTaskCreateTool(project_root=tmp_path, team_manager=manager))
    manager.list_board_tasks.return_value = []
    manager.create_board_task.return_value = {"id": "task1"}
    policy = {"allowlist": list(parent._tools)} if explicit_allow else None
    child = _team_registry(parent, tmp_path, policy)

    advertised = {s["function"]["name"] for s in child.get_openai_tools()}
    assert {"TeamStatus", "TeamApprovals", "TeamTaskList", "TeamTaskCreate"} <= advertised
    assert json.loads(child.execute_tool("TeamStatus", {"team_name": "demo"}))["status"] == "success"
    assert json.loads(child.execute_tool("TeamTaskList", {"team_name": "demo"}))["status"] == "success"
    assert json.loads(child.execute_tool("TeamTaskCreate", {"team_name": "demo", "subject": "work"}))["status"] == "success"
    manager.get_status.assert_called_once_with(team_name="demo")
    manager.create_board_task.assert_called_once()
    for control in controls:
        name = control.__name__.removesuffix("Tool")
        assert name not in advertised
        assert child.get_tool(name) is None
        assert json.loads(child.execute_tool(name, {}))["status"] == "error"
        assert parent.get_tool(name) is not None
    manager.respond_plan_approval.assert_not_called()
    manager.delete_team.assert_not_called()
    manager.create_team.assert_not_called()


@pytest.mark.parametrize("control,alias,kwargs,arguments", [
    (EnterPlanModeTool, "AliasPlan", "agent", {}),
    (ExitPlanModeTool, "AliasExitPlan", "agent", {"plan": "safe"}),
    (EnterWorktreeTool, "AliasWorktree", "workspace", {"path": "C:/example"}),
    (ExitWorktreeTool, "AliasExitWorktree", "workspace", {"action": "keep"}),
    (TeamApprovePlanTool, "AliasApproval", "team", {"team_name": "demo", "request_id": "req1", "approved": True}),
    (TeamCreateTool, "AliasCreate", "team", {"team_name": "demo"}),
    (TeamDeleteTool, "AliasDelete", "team", {"team_name": "demo"}),
    (TeamCleanupTool, "AliasCleanup", "team", {"team_name": "demo"}),
    (TeamFanoutTool, "AliasFanout", "team", {"team_name": "demo", "tasks": []}),
    (TeamRetryTool, "AliasRetry", "team", {"team_name": "demo", "work_id": "task1"}),
    (SendMessageTool, "AliasMessage", "team", {"team_name": "demo", "from_member": "lead", "to_member": "worker", "text": "approve", "type": "plan_approval_response", "request_id": "req1", "approved": True}),
])
def test_worker_rejects_aliased_parent_controls(control, alias, kwargs, arguments, tmp_path):
    agent = Mock()
    manager = Mock()
    manager.get_by_path.return_value = {"name": "demo", "path": str(tmp_path)}
    dependencies = {
        "agent": {"code_agent": agent},
        "workspace": {"code_agent": agent, "worktree_manager": manager},
        "team": {"team_manager": manager},
    }[kwargs]
    parent = ToolRegistry()
    parent.register_tool(control(name=alias, project_root=tmp_path, **dependencies))
    child = _team_registry(parent, tmp_path, {"allowlist": [alias]})

    assert alias not in {s["function"]["name"] for s in child.get_openai_tools()}
    assert child.get_tool(alias) is None
    assert json.loads(child.execute_tool(alias, arguments))["status"] == "error"
    assert parent.get_tool(alias) is not None
    agent.enter_plan_mode.assert_not_called()
    agent.enter_worktree.assert_not_called()
    assert agent.method_calls == []
    manager.respond_plan_approval.assert_not_called()
    manager.delete_team.assert_not_called()
    manager.send_message.assert_not_called()
    assert manager.method_calls == []


def test_derived_registry_keeps_shared_service_and_parent_live_fences(tmp_path):
    parent = ToolRegistry()
    shared_service = object()
    remote = MarkerTool("mcp__lookup", service=shared_service)
    parent.register_tool(remote, owner="mcp:test")
    state = ToolDiscoveryState()
    mode = {"allowed": True}
    parent.register_tool(ToolSearchTool(parent, state, can_promote=lambda _: True))
    parent.enable_deferred_discovery(state, can_execute=lambda _: mode["allowed"])
    child = _team_registry(parent, tmp_path)
    bound = child.get_tool("mcp__lookup")
    assert bound is not remote and bound.service is shared_service

    assert "mcp__lookup" not in {s["function"]["name"] for s in child.get_openai_tools()}
    assert json.loads(child.execute_tool("mcp__lookup", {"value": "a"}))["status"] == "error"
    parent.execute_tool("ToolSearch", {"query": "lookup"})
    parent.activate_discovered_tools()
    assert json.loads(child.execute_tool("mcp__lookup", {"value": "b"}))["status"] == "success"
    mode["allowed"] = False
    assert json.loads(child.execute_tool("mcp__lookup", {"value": "c"}))["status"] == "error"
    mode["allowed"] = True
    parent.replace_owned_tools("mcp:test", [MarkerTool("mcp__lookup")])
    assert json.loads(child.execute_tool("mcp__lookup", {"value": "d"}))["status"] == "error"
    assert bound.calls == [{"value": "b"}]


def test_parent_in_place_schema_change_revokes_existing_worker_binding(tmp_path):
    parent = ToolRegistry()
    source = MarkerTool("Read")
    parent.register_tool(source)
    child = _team_registry(parent, tmp_path)
    source.field = "different"
    assert json.loads(child.execute_tool("Read", {"value": "x"}))["status"] == "error"
    assert child.capture_tool_step().model_schemas() == []


def test_worker_calls_shared_mcp_transport_without_copying_client(tmp_path):
    class Client:
        def __init__(self):
            self.calls = []
            self.close_calls = 0

        def call_tool_sync(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            return SimpleNamespace(content=[], isError=False, structuredContent={"ok": True})

        def close_sync(self):
            self.close_calls += 1

    client = Client()
    parent = ToolRegistry()
    source = MCPToolAdapter(client, "mcp__lookup", "lookup", schema={
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    })
    parent.register_tool(source, owner="mcp:test")
    child = _team_registry(parent, tmp_path)
    bound = child.get_tool("mcp__lookup")
    assert bound is not source and bound._mcp_client is client
    step = child.capture_tool_step()
    assert json.loads(child.execute_tool("mcp__lookup", {"value": "x"}, snapshot=step))["status"] == "success"
    assert client.calls == [("lookup", {"value": "x"})]
    child.unregister_owner("mcp:test")
    assert client.close_calls == 0
    assert parent.get_tool("mcp__lookup") is source
    child.register_tool(MCPToolAdapter(client, "mcp__lookup", "unexpected", schema={
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    }), owner="mcp:test")
    assert json.loads(child.execute_tool("mcp__lookup", {"value": "blocked"}))["status"] == "error"
    assert json.loads(parent.execute_tool("mcp__lookup", {"value": "parent"}))["status"] == "success"
    assert client.calls[-1] == ("lookup", {"value": "parent"})


def test_task_worker_inherits_live_parent_mode_guard(tmp_path):
    parent = ToolRegistry()
    marker = MarkerTool("Read")
    parent.register_tool(marker)
    state = ToolDiscoveryState()
    allowed = {"yes": True}
    parent.register_tool(ToolSearchTool(parent, state, can_promote=lambda _: True))
    parent.enable_deferred_discovery(state, can_execute=lambda _: allowed["yes"])
    child = _task_registry(parent, tmp_path)
    allowed["yes"] = False
    assert json.loads(child.execute_tool("Read", {"value": "x"}))["status"] == "error"
    assert marker.calls == []


def test_parent_circuit_open_after_derivation_blocks_worker(tmp_path):
    parent = ToolRegistry()
    marker = MarkerTool("Read")
    parent.register_tool(marker)
    child = _team_registry(parent, tmp_path)
    for _ in range(parent._circuit_breaker.failure_threshold):
        parent._circuit_breaker.record_failure("Read", "down")
    assert child.capture_tool_step().model_schemas() == []
    assert json.loads(child.execute_tool("Read", {"value": "x"}))["status"] == "error"
    assert marker.calls == []


def test_sandbox_and_hosted_revocation_are_inherited(tmp_path):
    parent = ToolRegistry()
    bash = MarkerTool("Bash")
    read = MarkerTool("Read")
    parent.register_tool(bash)
    parent.register_tool(read)
    parent.restrict_to_tools({"Bash"})
    membership = {"active": True}
    authority = HostedToolAuthority(
        scope=MemoryScope(tenant_id="t", user_id="u", project_id="p"),
        check_membership=lambda _: membership["active"],
    )
    parent.bind_hosted_authority(authority)
    child = _team_registry(parent, tmp_path)
    assert child.get_tool("Read") is None
    assert json.loads(child.execute_tool("Bash", {"value": "a"}))["status"] == "success"
    membership["active"] = False
    assert json.loads(child.execute_tool("Bash", {"value": "b"}))["status"] == "error"
    assert child.get_tool("Bash") is None
    assert bash.calls == [{"value": "a"}]


def test_sandbox_activated_after_derivation_revokes_child(tmp_path):
    parent = ToolRegistry()
    tool = MarkerTool("Read")
    parent.register_tool(tool)
    child = _team_registry(parent, tmp_path)
    parent.restrict_to_tools({"Bash"})
    assert json.loads(child.execute_tool("Read", {"value": "x"}))["status"] == "error"
    assert tool.calls == []


def test_turn_executor_seals_schema_and_adapter_during_model_request(tmp_path):
    registry = ToolRegistry()
    old = MarkerTool("Read")
    registry.register_tool(old)
    replacement = MarkerTool("Read")
    seen = []

    def invoke(messages, *, tools, tool_choice):
        seen.extend(tools)
        registry.register_tool(replacement)
        return _response("Read", {"value": "x"})

    llm = SimpleNamespace(invoke_raw=invoke)
    executor = TurnExecutor(llm, registry, tmp_path)
    outcome = executor.execute_turn([{"role": "user", "content": "go"}], {})
    assert [s["function"]["name"] for s in seen] == ["Read"]
    assert _observation(outcome)["error"]["code"] == "STALE_TOOL_SCHEMA"
    assert old.calls == replacement.calls == []


def test_turn_executor_refreshes_schema_each_turn_and_validates_before_provider(tmp_path):
    registry = ToolRegistry()
    remote = MarkerTool("mcp__lookup")
    registry.register_tool(remote)
    calls = []

    def invoke(messages, *, tools, tool_choice):
        calls.append(tools)
        if len(calls) == 1:
            return _response("mcp__lookup", {"value": "a", "unknown": "unsafe"})
        return {"choices": [{"message": {"content": "done"}}]}

    executor = TurnExecutor(SimpleNamespace(invoke_raw=invoke), registry, tmp_path)
    first = executor.execute_turn([{"role": "user", "content": "go"}], {})
    assert _observation(first)["error"]["code"] == "INVALID_PARAM"
    assert remote.calls == []
    remote.field = "new_value"
    executor.execute_turn(first["messages"], {})
    assert "value" in calls[0][0]["function"]["parameters"]["properties"]
    assert "new_value" in calls[1][0]["function"]["parameters"]["properties"]
