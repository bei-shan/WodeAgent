"""Offline provider scripts exercise real Agent/session/team completion boundaries."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agents.codeAgent import CodeAgent
from core.background_task import BackgroundTaskRunner
from core.config import Config
from core.events import EventType
from core.llm_recovery import ModelInvocationError
from core.runtime.session_controller import AgentSession
from core.runtime.turn_state import load_turn_outcome
from core.team_engine.execution import ExecutionService
from core.team_engine.manager import TeamManager
from core.team_engine.turn_executor import TurnExecutor
from tools.builtin.task import TaskTool
from tools.builtin.task_output import TaskOutputTool
from tools.builtin.write_file import WriteTool
from tools.registry import ToolRegistry


class ScriptedProvider:
    model = "offline-scripted"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def invoke_raw(self, *args, **kwargs):
        self.calls += 1
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def raw(text="", reason="stop", calls=None):
    choice = {"message": {"content": text, "tool_calls": calls or []}}
    if reason is not None:
        choice["finish_reason"] = reason
    return {"choices": [choice]}


def call(name="missing_tool", arguments=None):
    return {"id": "call_a", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})}}


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_INTERACTIVE", "false")
    agents = []

    def create(responses):
        events = []
        registry = ToolRegistry()
        registry.register_tool(WriteTool(project_root=tmp_path))
        agent = CodeAgent(
            name="terminal-test", llm=ScriptedProvider(responses),
            tool_registry=registry, project_root=str(tmp_path),
            config=Config(llm_streaming=False, show_progress=False,
                          show_react_steps=False, max_steps=5),
            event_sink=SimpleNamespace(emit=events.append),
        )
        agents.append(agent)
        return agent, events

    yield create
    for agent in agents:
        agent.close()


@pytest.mark.parametrize("tool_calls", [[], [call("Write", {"path": "must-not-exist.txt", "content": "partial"})]])
@pytest.mark.parametrize("reason,kind", [("length", "output_truncated"), ("content_filter", "output_filtered")])
def test_incomplete_agent_turn_fails_without_final_or_tool_side_effects(agent_factory, tmp_path, tool_calls, reason, kind):
    agent, events = agent_factory([raw("partial evidence", reason, tool_calls), raw("done")])
    with pytest.raises(ModelInvocationError) as caught:
        agent.run("finish the whole task")
    assert caught.value.kind == kind
    assert not caught.value.retryable
    assert agent.llm.calls == 1
    assert agent.last_turn_status == "failed"
    assert agent.last_turn_error_kind == kind
    assert not agent._run_limit_hit
    assert not any(e.type in {EventType.ASSISTANT_FINAL, EventType.TOOL_STARTED} for e in events)
    assert events[-1].payload["status"] == "failed"
    history = agent.history_manager.get_messages()
    assert not any(m.metadata.get("action_type") == "final" for m in history)
    partial = [m for m in history if m.metadata.get("error_kind") == kind]
    assert partial and partial[-1].content == "partial evidence"
    saved_calls = partial[-1].metadata.get("tool_calls")
    assert len(saved_calls) == len(tool_calls)
    if tool_calls:
        assert saved_calls[0]["name"] == "Write"
    assert not (tmp_path / "must-not-exist.txt").exists()


@pytest.mark.parametrize("responses,kind", [
    ([raw("partial evidence", "length")], "output_truncated"),
    ([raw("partial evidence", "content_filter")], "output_filtered"),
    ([raw("partial evidence", "content_filter", [call("Write")])], "output_filtered"),
    ([raw("", "content_filter")], "output_filtered"),
    ([raw(), raw()], "empty_response"),
])
def test_session_persists_honest_failure(agent_factory, tmp_path, responses, kind):
    agent, events = agent_factory(responses)
    session = AgentSession("abc123def456", lambda: agent, workspace_base=str(tmp_path))
    session._agent = agent
    session._run_turn("finish the whole task")
    assert session.last_turn.status.value == "failed"
    assert session.last_turn.error_kind == kind
    assert load_turn_outcome(session.session_id).error_kind == kind
    assert agent.last_turn_error_kind == kind
    assert not agent._run_limit_hit
    assert agent.llm.calls == len(responses)
    assert not any(e.type == EventType.ASSISTANT_FINAL for e in events)


@pytest.mark.parametrize("reason", ["stop", None])
def test_agent_valid_final_preserves_provider_compatibility(agent_factory, reason):
    agent, events = agent_factory([raw("done", reason)])
    assert agent.run("finish") == "done"
    assert agent.last_turn_status == "completed"
    assert agent.last_turn_error_kind is None
    assert [e.payload["content"] for e in events if e.type == EventType.ASSISTANT_FINAL] == ["done"]


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_agent_can_finish_new_turn_after_incomplete_response_without_replaying_calls(agent_factory, reason):
    agent, events = agent_factory([raw("partial", reason, [call("Write")]), raw("done")])
    with pytest.raises(ModelInvocationError):
        agent.run("finish")
    assert agent.run("continue") == "done"
    assert agent.last_turn_status == "completed"
    assert agent.last_turn_error_kind is None
    assert not any(e.type == EventType.TOOL_STARTED for e in events)


def test_empty_reply_retry_can_still_produce_valid_final(agent_factory):
    agent, events = agent_factory([raw(), raw("done")])
    assert agent.run("finish") == "done"
    assert agent.llm.calls == 2
    assert agent.last_turn_status == "completed"


def test_agent_tool_error_can_recover_to_valid_final(agent_factory):
    agent, events = agent_factory([raw(calls=[call()]), raw("recovered")])
    assert agent.run("finish") == "recovered"
    assert agent.last_turn_status == "completed"
    assert len([e for e in events if e.type == EventType.ASSISTANT_FINAL]) == 1


@pytest.mark.parametrize("tool_calls", [[], [call("side_effect")]])
@pytest.mark.parametrize("object_response", [False, True])
@pytest.mark.parametrize("reason,kind", [("length", "output_truncated"), ("content_filter", "output_filtered")])
def test_turn_executor_rejects_incomplete_response_before_tool_execution(tmp_path, tool_calls, object_response, reason, kind):
    response = raw("partial", reason, tool_calls)
    if object_response:
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=reason, message=SimpleNamespace(content="partial", tool_calls=tool_calls))])
    registry = ToolRegistry()
    invoked = []
    registry.register_tool(SimpleNamespace(name="side_effect", description="effect",
        get_parameters=lambda: [], run=lambda args: invoked.append(args)))
    provider = ScriptedProvider([response, raw("done")])
    executor = TurnExecutor(provider, registry, tmp_path)
    with pytest.raises(ModelInvocationError) as caught:
        executor.execute_turn([], {})
    assert caught.value.kind == kind
    assert not caught.value.retryable
    assert provider.calls == 1
    assert caught.value.partial_messages[-1]["content"] == "partial"
    assert len(caught.value.partial_messages[-1].get("tool_calls", [])) == len(tool_calls)
    assert not invoked


@pytest.mark.parametrize("reason", ["stop", None])
def test_turn_executor_valid_final_preserves_provider_compatibility(tmp_path, reason):
    executor = TurnExecutor(ScriptedProvider([raw("done", reason)]), ToolRegistry(), tmp_path)
    result = executor.execute_turn([], {})
    assert result["done"] is True
    assert result["final_result"] == "done"


def test_turn_executor_empty_is_failure(tmp_path):
    executor = TurnExecutor(ScriptedProvider([raw("  ")]), ToolRegistry(), tmp_path)
    with pytest.raises(ModelInvocationError) as caught:
        executor.execute_turn([], {})
    assert caught.value.kind == "empty_response"


def test_filtered_error_message_explains_provider_blocking():
    error = ModelInvocationError("output_filtered")
    assert "模型服务" in str(error)
    assert "拦截" in str(error)
    assert "任务未完成" in str(error)
    assert not error.retryable


@pytest.mark.parametrize("responses,max_steps,expected,expected_kind", [
    ([raw(calls=[call()])], 1, "failed", "step_limit"),
    ([raw(calls=[call()]), raw()], 5, "failed", "empty_response"),
    ([raw("partial", "length")], 5, "failed", "output_truncated"),
    ([raw("partial", "content_filter")], 5, "failed", "output_filtered"),
    ([raw("partial", "content_filter", [call("effect")])], 5, "failed", "output_filtered"),
    ([raw("done", "stop")], 5, "succeeded", None),
    ([raw("done", None)], 5, "succeeded", None),
    ([raw(calls=[call()]), raw("recovered and completed")], 5, "succeeded", None),
])
def test_real_team_manager_persists_work_and_board_outcome(tmp_path, monkeypatch, responses, max_steps, expected, expected_kind):
    monkeypatch.setenv("TEAM_WORKER_MAX_STEPS", str(max_steps))
    provider = ScriptedProvider(responses)
    effects = []
    registry = ToolRegistry()
    registry.register_tool(SimpleNamespace(name="effect", description="effect", get_parameters=lambda: [],
                                         run=lambda args: effects.append(args) or "changed"))
    manager = TeamManager(str(tmp_path), llm=provider, tool_registry=registry)
    try:
        manager.create_team("audit", members=[{"name": "lead"}, {"name": "dev"}])
        task = manager.create_board_task("audit", subject="finish work", description="finish")
        item = manager.store.create_work_item("audit", owner="dev", title="finish",
            instruction="finish", payload={"board_task_id": task["id"]})
        assert manager._process_next_work_item("audit", "dev")
        persisted = manager.store.list_work_items("audit")[0]
        assert persisted["work_id"] == item["work_id"]
        assert persisted["status"] == expected
        assert provider.calls == len(responses)
        assert not effects
        snapshot = manager.collect_work("audit")
        assert snapshot["counts"][expected] == 1
        assert manager.get_board_task("audit", task["id"])["status"] == (
            "completed" if expected == "succeeded" else "canceled")
        if expected == "failed":
            assert persisted.get("error")
            assert persisted["error"]["kind"] == expected_kind
            assert persisted["error"]["retryable"] is False
            # Partial model/tool observations remain private diagnostic evidence.
            assert "partial_messages" not in persisted["error"]
            assert "partial" not in json.dumps(persisted["error"])
            assert not persisted.get("result")
    finally:
        manager.shutdown()


@pytest.mark.parametrize("provider_error", [ConnectionError("connection timeout"),
                                          ModelInvocationError("network", retryable=True)])
def test_worker_never_replays_task_after_tool_side_effects(tmp_path, monkeypatch, provider_error):
    monkeypatch.setenv("TEAM_LLM_RETRY_BACKOFF", "0")
    llm = ScriptedProvider([raw(calls=[call("effect")]), provider_error,
                            raw(calls=[call("effect")]), raw("done")])
    effects = []
    registry = ToolRegistry()
    registry.register_tool(SimpleNamespace(name="effect", description="effect", get_parameters=lambda: [],
                                         run=lambda args: effects.append(args) or "changed"))
    service = ExecutionService(project_root=str(tmp_path), llm=llm, tool_registry=registry,
        work_executor=None, read_team_fn=lambda _: {"members": []})
    with pytest.raises(ModelInvocationError) as caught:
        service.execute_work_item("audit", "dev", {"instruction": "change"})
    assert not caught.value.retryable
    assert len(effects) == 1
    assert llm.calls == 2
    assert any(m.get("role") == "tool" for m in caught.value.partial_messages)


@pytest.mark.parametrize("boundary", ["foreground", "persisted", "retrieved"])
@pytest.mark.parametrize("response,max_steps,kind,retryable", [
    (raw("PRIVATE_PARTIAL", "length"), 5, "output_truncated", False),
    (raw(), 5, "empty_response", False),
    (raw(calls=[call("Read")]), 1, "step_limit", False),
    (raw("PRIVATE_PARTIAL", "content_filter"), 5, "output_filtered", False),
    (raw("PRIVATE_PARTIAL", "content_filter", [call("Read")]), 5, "output_filtered", False),
    (ModelInvocationError("network", retryable=True,
                          partial_messages=[{"role": "tool", "content": "PRIVATE_OBSERVATION"}]),
     5, "network", True),
])
def test_task_tool_reports_incomplete_execution_as_failure(tmp_path, response, max_steps, kind, retryable, boundary):
    if isinstance(response, ModelInvocationError):
        response.provider_payload = "PRIVATE_PROVIDER"
    provider = ScriptedProvider([response])
    effects = []
    registry = ToolRegistry()
    registry.register_tool(SimpleNamespace(name="Read", description="effect", get_parameters=lambda: [],
                                         run=lambda args: effects.append(args) or "PRIVATE_OBSERVATION"))
    tool = TaskTool(project_root=tmp_path, main_llm=provider, tool_registry=registry)
    tool._subagent_max_steps = max_steps
    runner = BackgroundTaskRunner(tmp_path)
    tool._background_runner = runner
    result = json.loads(tool.run({"description": "finish", "prompt": "finish", "model": "main",
                                 "run_in_background": boundary != "foreground"}))
    if boundary == "foreground":
        assert result["status"] == "error"
        assert result["error"]["code"] == "INTERNAL_ERROR"
        public = result["data"]
    else:
        assert result["status"] == "success"
        task_id = result["data"]["task_id"]
        deadline = time.monotonic() + 3
        while runner.get_result(task_id) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        persisted = json.loads((tmp_path / ".tasks/output" / f"{task_id}.json").read_text(encoding="utf-8"))
        assert persisted == runner.get_result(task_id)
        assert persisted["status"] == "failed"
        assert persisted["result"]
        assert persisted["tool_usage"] == {}
        public = persisted
        if boundary == "retrieved":
            # Reopen the disk result with a new runner; no in-memory exception is available.
            output = TaskOutputTool(project_root=tmp_path, background_runner=BackgroundTaskRunner(tmp_path))
            result = json.loads(output.run({"task_id": task_id}))
            assert result["status"] == "success"
            public = result["data"]
            for field in ("status", "result", "tool_usage", "finished_at"):
                assert public[field] == persisted[field]
        assert "PRIVATE_PARTIAL" not in json.dumps(persisted)
        assert "PRIVATE_OBSERVATION" not in json.dumps(persisted)
        assert "partial_messages" not in json.dumps(persisted)
    assert provider.calls == 1
    assert len(effects) == (1 if kind == "step_limit" else 0)
    assert "PRIVATE_PARTIAL" not in json.dumps(result)
    assert "PRIVATE_OBSERVATION" not in json.dumps(result)
    assert "PRIVATE_PROVIDER" not in json.dumps(result)
    assert "partial_messages" not in json.dumps(result)
    assert public["error"] == {"kind": kind, "retryable": retryable}


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("reason", ["stop", None])
def test_task_tool_valid_final_preserves_provider_compatibility(tmp_path, background, reason):
    provider = ScriptedProvider([raw("done", reason)])
    tool = TaskTool(project_root=tmp_path, main_llm=provider, tool_registry=ToolRegistry())
    runner = BackgroundTaskRunner(tmp_path)
    tool._background_runner = runner
    result = json.loads(tool.run({"description": "finish", "prompt": "finish", "model": "main",
                                 "run_in_background": background}))
    if not background:
        assert result["status"] == "success"
        assert result["data"]["result"] == "done"
        assert "error" not in result["data"]
    else:
        task_id = result["data"]["task_id"]
        deadline = time.monotonic() + 3
        while runner.get_result(task_id) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner.get_result(task_id)["status"] == "completed"
        assert runner.get_result(task_id)["result"] == "done"
        assert "error" not in runner.get_result(task_id)
        output = TaskOutputTool(project_root=tmp_path, background_runner=runner)
        retrieved = json.loads(output.run({"task_id": task_id}))
        assert retrieved["status"] == "success"
        assert retrieved["data"]["status"] == "completed"
        assert retrieved["data"]["result"] == "done"
        assert "error" not in retrieved["data"]
    assert provider.calls == 1
