"""Integration checks for CodeAgent multi-tool dispatch without LLM/network."""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

from agents.codeAgent import CodeAgent
from core.events import EventType


class RecordingTrace:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload, step=0):
        self.events.append((name, payload, step))


class RecordingHistory:
    def __init__(self):
        self.assistants = []
        self.tools = []

    def append_assistant(self, **kwargs):
        self.assistants.append(kwargs)

    def append_tool(self, **kwargs):
        self.tools.append(kwargs)


def make_agent(*, parallel=True, feature=None):
    agent = CodeAgent.__new__(CodeAgent)
    agent.config = SimpleNamespace(
        tool_parallel_reads_enabled=parallel, tool_max_concurrency=3,
        budget_enforce=False,
    )
    agent._tool_interceptors = [feature] if feature is not None else []
    agent._hook_manager = None
    agent.delegate_mode = False
    agent._interrupt_requested = threading.Event()
    agent.history_manager = RecordingHistory()
    agent.project_root = "."
    agent.event_records = []
    agent.event_sink = SimpleNamespace(emit=agent.event_records.append)
    agent.logger = logging.getLogger("test-step-tool-batches")
    agent.console_verbose = False
    agent._log_message_write = lambda *args: None
    agent.tool_registry = SimpleNamespace(get_tool=lambda name: SimpleNamespace(
        is_concurrency_safe=lambda _: name in {"LS", "Glob", "Grep"},
    ))
    return agent


def call(name, ident):
    return {"name": name, "id": ident, "arguments": {"path": "."}}


def test_codeagent_overlaps_safe_reads_but_preserves_tool_history_order():
    agent = make_agent()
    lock = threading.Lock()
    current, peak = 0, 0

    def execute(name, params):
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.04)
        with lock:
            current -= 1
        return json.dumps({"status": "success", "data": {"tool": name}})

    agent._execute_tool = execute
    trace = RecordingTrace()
    agent._execute_step_tools([call("LS", "x"), call("Glob", "y"), call("Bash", "z")], "", None, trace, 1)

    assert peak == 2
    assert [item["metadata"]["tool_call_id"] for item in agent.history_manager.tools] == ["x", "y", "z"]
    batches = [payload for name, payload, _ in trace.events if name == "tool_batch_started"]
    assert [(b["mode"], b["count"]) for b in batches] == [("parallel", 2), ("serial", 1)]
    events = [e.type for e in agent.event_records]
    assert events[:4] == [EventType.TOOL_STARTED, EventType.TOOL_STARTED,
                          EventType.TOOL_COMPLETED, EventType.TOOL_COMPLETED]


def test_unknown_interceptor_forces_serial_execution():
    agent = make_agent(feature=SimpleNamespace(name="unknown_plugin"))
    threads = []
    parent_thread = threading.get_ident()
    agent._execute_tool = lambda name, params: (
        threads.append(threading.get_ident()) or '{"status":"success"}'
    )
    trace = RecordingTrace()
    agent._execute_step_tools([call("LS", "x"), call("Glob", "y")], "", None, trace, 1)
    assert threads == [parent_thread, parent_thread]
    assert all(payload["mode"] == "serial" for name, payload, _ in trace.events if name == "tool_batch_started")


def test_invalid_arguments_return_result_for_original_tool_call():
    agent = make_agent()
    agent._execute_tool = lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch"))
    trace = RecordingTrace()
    agent._execute_step_tools([
        {"name": "LS", "id": "bad", "arguments": "{not-json"},
    ], "", None, trace, 1)
    result = json.loads(agent.history_manager.tools[0]["raw_result"])
    assert result["error"]["code"] == "INVALID_PARAM"
    assert agent.history_manager.tools[0]["metadata"]["tool_call_id"] == "bad"


def test_interrupt_completes_all_announced_calls_without_dispatch():
    agent = make_agent()
    agent._interrupt_requested.set()
    agent._execute_tool = lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch"))
    agent._execute_step_tools([call("LS", "x"), call("Glob", "y")], "", None, RecordingTrace(), 1)
    assert [json.loads(t["raw_result"])["error"]["code"] for t in agent.history_manager.tools] == [
        "INTERRUPTED", "INTERRUPTED",
    ]
