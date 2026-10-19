"""Real Agent mode transitions apply to already derived worker capabilities."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.codeAgent import CodeAgent
from core.config import Config
from core.team_engine.execution import ExecutionService
from core.team_engine.turn_executor import TurnExecutor
from tools.builtin.task import TaskTool
from tools.mcp.adapter import MCPToolAdapter
from tools.registry import ToolRegistry


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    # Exercise real bootstrap and policies without any MCP connection or model.
    monkeypatch.setattr("tools.mcp.loader.register_mcp_servers", lambda *a, **k: ([], []))
    agents = []

    def create(deferred):
        agent = CodeAgent(
            name="lead", llm=SimpleNamespace(), tool_registry=ToolRegistry(),
            project_root=str(tmp_path), config=Config(
                enable_agent_teams=True, tool_deferred_discovery_enabled=deferred,
            ),
        )
        agents.append(agent)
        return agent

    yield create
    for agent in reversed(agents):
        agent.close()


def team_worker(agent, tmp_path, policy=None):
    member = {"name": "worker", "role": "developer", "tool_policy": policy or {}}
    service = ExecutionService(
        project_root=str(tmp_path), llm=agent.llm, tool_registry=agent.tool_registry,
        work_executor=None, read_team_fn=lambda _: {"members": [member]},
    )
    return service._build_teammate_registry("team", "worker")[0]


def task_worker(agent, tmp_path):
    return TaskTool(
        project_root=tmp_path, main_llm=agent.llm, tool_registry=agent.tool_registry,
    )._create_filtered_registry()


def names(schemas):
    return {item["function"]["name"] for item in schemas}


def dispatch(registry, name, arguments, snapshot=None):
    return json.loads(registry.execute_tool(name, arguments, snapshot=snapshot))


def worker_turn(registry, tmp_path, name, arguments, before_response=None):
    seen = []

    def invoke(messages, *, tools, tool_choice):
        seen.extend(tools)
        if before_response:
            before_response()
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }}],
        }}]}

    outcome = TurnExecutor(SimpleNamespace(invoke_raw=invoke), registry, tmp_path).execute_turn(
        [{"role": "user", "content": "perform delegated work"}], {},
    )
    observation = json.loads(next(m["content"] for m in outcome["messages"] if m["role"] == "tool"))
    return observation, seen


@pytest.mark.parametrize("deferred", [False, True])
def test_existing_team_worker_obeys_live_plan_mode_and_authorized_exit(agent_factory, tmp_path, deferred):
    agent = agent_factory(deferred)
    child = team_worker(agent, tmp_path)
    assert dispatch(child, "Write", {"path": "before.txt", "content": "before"})["status"] == "success"
    stale = child.capture_tool_step()
    assert "Write" in names(stale.model_schemas())

    agent.enter_plan_mode()
    assert "Write" not in names(agent._get_openai_tools_for_current_mode())
    assert "Write" not in names(child.capture_tool_step().model_schemas())
    assert json.loads(agent._execute_tool("Write", {"path": "parent.txt", "content": "blocked"}))["status"] == "error"
    for snapshot in (None, stale):
        blocked = dispatch(child, "Write", {"path": "blocked.txt", "content": "blocked"}, snapshot)
        assert blocked["error"]["code"] == "PERMISSION_DENIED"
    blocked, seen = worker_turn(child, tmp_path, "Write", {"path": "turn.txt", "content": "blocked"})
    assert "Write" not in names(seen)
    assert blocked["status"] == "error"
    assert dispatch(child, "Read", {"path": "before.txt"}, child.capture_tool_step())["status"] == "success"
    assert (tmp_path / "before.txt").read_text(encoding="utf-8") == "before"
    assert all(not (tmp_path / name).exists() for name in ("parent.txt", "blocked.txt", "turn.txt"))

    agent.exit_plan_mode("1. Implement the approved change")
    result, seen = worker_turn(child, tmp_path, "Write", {"path": "after.txt", "content": "after"})
    assert "Write" in names(seen) and result["status"] == "success"
    assert (tmp_path / "after.txt").read_text(encoding="utf-8") == "after"


@pytest.mark.parametrize("deferred", [False, True])
def test_worker_turn_rechecks_plan_entry_during_model_request(agent_factory, tmp_path, deferred):
    agent = agent_factory(deferred)
    child = team_worker(agent, tmp_path)
    result, seen = worker_turn(
        child, tmp_path, "Write", {"path": "raced.txt", "content": "blocked"},
        before_response=agent.enter_plan_mode,
    )
    assert "Write" in names(seen)
    assert result["status"] == "error"
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert not (tmp_path / "raced.txt").exists()


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("derive", [team_worker, task_worker])
def test_delegate_mode_restricts_lead_but_keeps_worker_read_policy(agent_factory, tmp_path, deferred, derive):
    agent = agent_factory(deferred)
    child = derive(agent, tmp_path)
    stale = child.capture_tool_step()
    agent.set_delegate_mode(True)
    (tmp_path / "input.txt").write_text("delegated input", encoding="utf-8")

    assert "Write" not in names(agent._get_openai_tools_for_current_mode())
    assert json.loads(agent._execute_tool("Write", {"path": "lead.txt", "content": "blocked"}))["status"] == "error"
    assert dispatch(agent.tool_registry, "Write", {"path": "lead-direct.txt", "content": "blocked"})["status"] == "error"
    assert "Read" in names(child.capture_tool_step().model_schemas())
    assert dispatch(child, "Read", {"path": "input.txt"}, stale)["status"] == "success"
    result, seen = worker_turn(child, tmp_path, "Read", {"path": "input.txt"})
    assert "Read" in names(seen) and result["status"] == "success"

    if derive is team_worker:
        result, seen = worker_turn(child, tmp_path, "Write", {"path": "worker.txt", "content": "implemented"})
        assert "Write" in names(seen) and result["status"] == "success"
        assert (tmp_path / "worker.txt").read_text(encoding="utf-8") == "implemented"
    else:
        assert "Write" not in names(child.capture_tool_step().model_schemas())
        assert dispatch(child, "Write", {"path": "task.txt", "content": "blocked"})["status"] == "error"
        assert not (tmp_path / "task.txt").exists()
    assert not (tmp_path / "lead.txt").exists() and not (tmp_path / "lead-direct.txt").exists()

    # Planning remains shared even while the leader's delegate restriction is local.
    agent.enter_plan_mode()
    assert dispatch(child, "Read", {"path": "input.txt"}, stale)["status"] == "success"
    assert dispatch(child, "Write", {"path": "plan.txt", "content": "blocked"}, stale)["status"] == "error"
    assert not (tmp_path / "plan.txt").exists()
    assert not {"Task", "EnterPlanMode", "ExitPlanMode", "AskUser"} & names(child.get_openai_tools())


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("policy", [{"allowlist": ["Read"]}, {"denylist": ["Write"]}])
def test_delegate_worker_retains_explicit_policy(agent_factory, tmp_path, deferred, policy):
    agent = agent_factory(deferred)
    agent.set_delegate_mode(True)
    child = team_worker(agent, tmp_path, policy)
    assert "Read" in names(child.get_openai_tools())
    assert "Write" not in names(child.get_openai_tools())
    assert dispatch(child, "Write", {"path": "denied.txt", "content": "blocked"})["status"] == "error"
    assert not (tmp_path / "denied.txt").exists()


def test_delegate_worker_cannot_bypass_deferred_mcp_visibility(agent_factory, tmp_path):
    agent = agent_factory(True)
    calls = []
    client = SimpleNamespace(call_tool_sync=lambda *a: calls.append(a))
    agent.tool_registry.register_tool(MCPToolAdapter(
        client, "mcp__hidden", "hidden", schema={"type": "object", "properties": {}},
    ), owner="mcp:offline")
    child = team_worker(agent, tmp_path)
    agent.set_delegate_mode(True)
    assert "mcp__hidden" not in names(child.capture_tool_step().model_schemas())
    assert dispatch(child, "mcp__hidden", {})["error"]["code"] == "DISCOVERY_REQUIRED"
    assert calls == []
