"""ToolRuntime contract: ordering, early denial, output and error semantics."""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from core.runtime.tool_runtime import ToolRuntime
from agents.codeAgent import CodeAgent


class FakeDispatcher:
    def __init__(self, events):
        self.events = events
    def execute_tool(self, name, parameters):
        self.events.append(("dispatch", name, dict(parameters)))
        return '{"status":"success"}'


class FakeFeature:
    def __init__(self, name, events, *, update=None, blocked=False):
        self.name = name
        self.events = events
        self.update = update
        self.blocked = blocked
    def pre_tool_use(self, agent, name, params):
        self.events.append(("before", self.name, dict(params)))
        if self.blocked:
            return {"blocked": True, "error_code": "DENIED",
                    "reason": "blocked by policy"}
        return {"updated_input": self.update or {},
                "system_messages": [f"pre:{self.name}"]}
    def post_tool_use(self, agent, name, params, output):
        self.events.append(("after", self.name, dict(params)))
        return [f"post:{self.name}"]


def test_feature_order_and_updated_parameters_and_messages():
    events, messages, times = [], [], {}
    features = [
        FakeFeature("a", events, update={"value": 1}),
        FakeFeature("b", events, update={"value": 2}),
    ]
    rt = ToolRuntime(FakeDispatcher(events), logging.getLogger("test"))
    result = rt.execute(
        "Read", {"path": "hello"},
        agent=object(), features=iter(features),
        emit_messages=messages.extend,
        record_timing=times.__setitem__,
    )
    assert result == '{"status":"success"}'
    assert [event[:2] for event in events] == [
        ("before", "a"), ("before", "b"),
        ("dispatch", "Read"), ("after", "a"), ("after", "b"),
    ]
    assert events[2][2] == {"path": "hello", "value": 2}
    assert messages == ["pre:a", "pre:b", "post:a", "post:b"]
    assert isinstance(times["Read"], int) and times["Read"] >= 0


def test_blocked_feature_stops_tool_dispatch_and_preserves_existing_messages():
    events, messages, times = [], [], {}
    rt = ToolRuntime(FakeDispatcher(events), logging.getLogger("test"))
    output = rt.execute(
        "Bash", "echo foo", agent=object(),
        features=[
            FakeFeature("one", events),
            FakeFeature("policy", events, blocked=True),
            FakeFeature("never", events),
        ],
        emit_messages=messages.extend,
        record_timing=times.__setitem__,
    )
    body = json.loads(output)
    assert body == {
        "status": "error", "data": {},
        "error": {"code": "DENIED", "message": "blocked by policy"},
        "stats": {"time_ms": 0},
    }
    assert events == [
        ("before", "one", {"input": "echo foo"}),
        ("before", "policy", {"input": "echo foo"}),
    ]
    assert messages == ["pre:one"]
    assert times == {}


def test_dispatch_error_is_not_swallowed():
    events = []
    class BrokenDispatcher:
        def execute_tool(self, name, params):
            events.append("dispatch")
            raise RuntimeError("failed")
    rt = ToolRuntime(BrokenDispatcher(), logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="failed"):
        rt.execute("Read", {}, agent=object(), features=[],
                   emit_messages=lambda _: None, record_timing=lambda *_: None)
    assert events == ["dispatch"]


def test_codeagent_delegates_to_tool_runtime_without_full_engine():
    agent = CodeAgent.__new__(CodeAgent)
    events = []
    agent.tool_registry = FakeDispatcher(events)
    agent.logger = logging.getLogger("test")
    agent._features = [FakeFeature("one", events)]
    agent._hook_system_messages = []
    result = agent._execute_tool("Read", {"path": "."})
    assert result == '{"status":"success"}'
    assert agent._hook_system_messages == ["pre:one", "post:one"]
    assert agent._last_tool_timing["Read"] >= 0


def test_runtime_imports_no_presentation_or_codeagent():
    source = (Path(__file__).resolve().parents[1] /
              "core/runtime/tool_runtime.py").read_text(encoding="utf-8")
    root = ast.parse(source)
    for node in ast.walk(root):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith((
                "agents", "desktop", "tui",
            ))
