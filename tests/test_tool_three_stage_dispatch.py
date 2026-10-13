"""Policy decisions and publication never run in worker threads."""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

from agents.codeAgent import CodeAgent


class Trace:
    def __init__(self):
        self.events = []
    def log_event(self, name, payload, step=0):
        self.events.append((name, payload))


class History:
    def __init__(self):
        self.records = []
    def append_assistant(self, **kwargs):
        pass
    def append_tool(self, **kwargs):
        self.records.append(kwargs)


def make_agent(registry, features):
    agent = CodeAgent.__new__(CodeAgent)
    agent.config = SimpleNamespace(
        tool_parallel_reads_enabled=True, tool_max_concurrency=3,
        budget_enforce=False,
    )
    agent._features = features
    agent._hook_manager = None
    agent.delegate_mode = False
    agent._interrupt_requested = threading.Event()
    agent._hook_system_messages = []
    agent.history_manager = History()
    agent.project_root = "."
    agent.event_sink = SimpleNamespace(emit=lambda *_: None)
    agent.logger = logging.getLogger("tool-stages-test")
    agent.console_verbose = False
    agent._log_message_write = lambda *_: None
    agent.tool_registry = registry
    return agent


def call(name="LS", ident="id"):
    return {"name": name, "id": ident, "arguments": {"path": "."}}


def test_pre_and_post_features_only_run_on_agent_owner_thread():
    owner = threading.get_ident()
    records = []
    class Interceptor:
        name = "policy"
        def pre_tool_use(self, agent, name, params):
            records.append(("pre", threading.get_ident()))
            return {"updated_input": {"directory": "."}, "system_messages": ["pre"]}
        def post_tool_use(self, agent, name, params, output):
            records.append(("post", threading.get_ident()))
            return ["post"]

    class Registry:
        def get_tool(self, _):
            return SimpleNamespace(is_concurrency_safe=lambda _: True)
        def execute_tool(self, *_):
            records.append(("execute", threading.get_ident()))
            return '{"status":"success"}'
    agent = make_agent(Registry(), [Interceptor()])
    agent._execute_step_tools([call()], "", None, Trace(), 1)
    assert [k for k, _ in records] == ["pre", "execute", "post"]
    assert all(thread == owner for _, thread in records)
    assert agent._hook_system_messages == ["pre", "post"]
    assert len(agent.history_manager.records) == 1


def test_denied_tool_never_dispatches_and_still_publishes_result():
    class Policy:
        name = "policy"
        def pre_tool_use(self, *_):
            return {"blocked": True, "error_code": "DENIED", "reason": "policy"}
        def post_tool_use(self, *_):
            raise AssertionError("post hook should not run when denied")
    class Registry:
        def get_tool(self, _):
            return None
        def execute_tool(self, *_):
            raise AssertionError("denied tool reached dispatcher")
    agent = make_agent(Registry(), [Policy()])
    trace = Trace()
    agent._execute_step_tools([call("Bash")], "", None, trace, 1)
    result = json.loads(agent.history_manager.records[0]["raw_result"])
    assert result["error"]["code"] == "DENIED"
    assert agent.history_manager.records[0]["metadata"]["tool_call_id"] == "id"


def test_parallel_read_dispatch_workers_and_owner_result_publication():
    owner = threading.get_ident()
    calls = []
    lock = threading.Lock()
    running = [0, 0]
    class Registry:
        def get_tool(self, name):
            return SimpleNamespace(is_concurrency_safe=lambda _: name in {"LS", "Glob"})
        def execute_tool(self, name, params):
            assert threading.get_ident() != owner
            with lock:
                running[0] += 1
                running[1] = max(running[0], running[1])
            time.sleep(0.035)
            with lock:
                running[0] -= 1
            calls.append((name, params))
            return '{"status":"success"}'
    agent = make_agent(Registry(), [])
    trace = Trace()
    agent._execute_step_tools(
        [call("LS", "one"), call("Glob", "two")], "", None, trace, 1,
    )
    assert running[1] == 2
    assert [item["metadata"]["tool_call_id"] for item in agent.history_manager.records] == ["one", "two"]
    assert any(name == "tool_batch_started" and obj["mode"] == "parallel" for name, obj in trace.events)
