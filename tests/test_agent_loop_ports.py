"""AgentLoop and ContextRuntime semantics with fake ports, no model or network."""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from core.events import EventType
from core.runtime.agent_loop import AgentLoop
from core.runtime.context_runtime import ContextRuntime


class Trace:
    def __init__(self): self.logs = []
    def log_event(self, kind, payload, step=0): self.logs.append((kind, step, payload))


class History:
    def __init__(self): self.saved = []
    def append_assistant(self, **kwargs): self.saved.append(kwargs)


class Host:
    def __init__(self, results):
        self.max_steps = 3
        self._interrupt_requested = Event()
        self._run_limit_hit = False
        self.event_sink = SimpleNamespace(emit=lambda event: self.events.append(event))
        self.history_manager = History()
        self.logger = logging.getLogger("fake-loop-port")
        self.console_verbose = False
        self.console_progress = False
        self.events = []
        self.results = iter(results)
        self.actions = []

    def _console(self, text): raise AssertionError(text)
    def _get_openai_tools_for_current_mode(self): return [{"function": {"name": "Read"}}]
    def _collect_runtime_blocks(self, step): self.actions.append(("runtime", step))
    def _maybe_compress_history(self, pending_input, trace, step, tools_schema):
        self.actions.append(("context", step))
    def _build_step_messages(self, trace, step):
        return ([{"role": "user", "content": "hi"}], [{"role": "user", "content": "hi"}])
    def _invoke_llm_with_retry(self, *args): return next(self.results)
    def _execute_step_tools(self, calls, text, reason, trace, step):
        self.actions.append(("tools", step, calls))
    def _log_message_write(self, trace, role, content, meta, step):
        self.actions.append(("written", step, content))


def result(text="", calls=None):
    return {"response_text": text, "tool_calls": calls or [], "reasoning_content": None}


def test_agent_loop_preserves_step_order_and_final_events():
    host = Host([result("working", [{"name": "Read", "id": "x"}]), result("done")])
    trace = Trace()
    assert AgentLoop(host).run("input", False, trace) == "done"
    assert host.actions[:4] == [
        ("runtime", 1), ("context", 1),
        ("tools", 1, [{"name": "Read", "id": "x"}]), ("runtime", 2),
    ]
    assert [event.type for event in host.events] == [
        EventType.STEP_STARTED, EventType.STEP_STARTED, EventType.ASSISTANT_FINAL,
    ]
    assert host.history_manager.saved[0]["content"] == "done"
    assert host._run_limit_hit is False
    assert trace.logs[-1][0] == "finish"


def test_cancelled_loop_emits_no_spurious_steps_or_assistant():
    host = Host([])
    host._interrupt_requested.set()
    assert "停止" in AgentLoop(host).run("x", False, Trace())
    assert host.events == []
    assert not host._run_limit_hit


def test_exhausted_steps_mark_limit_hit():
    host = Host([result("thinking", [{"name": "Read"}])] * 3)
    assert "限定步数" in AgentLoop(host).run("x", False, Trace())
    assert host._run_limit_hit is True
    assert len([e for e in host.events if e.type == EventType.STEP_STARTED]) == 3


class FakeContextHost:
    def __init__(self, compress):
        self.logger = logging.getLogger("fake-context-port")
        self.logger.setLevel(logging.WARNING)
        self.console_verbose = False
        self.config = SimpleNamespace(context_window=100, compression_threshold=0.8)
        self.history_manager = SimpleNamespace(
            should_compress=lambda pending, **kwargs: compress,
            estimate_context_tokens=lambda _: 91,
            get_total_usage_tokens=lambda: 20,
            get_message_count=lambda: 8 if compress else 3,
            get_rounds_count=lambda: 4,
            compact=lambda **kwargs: {"compressed": True, "summary_generated": True},
            to_messages=lambda: [{"role": "user", "content": "hello"}],
        )
        self.context_builder = SimpleNamespace(
            build_messages=lambda history: [{"role": "system", "content": "instructions"}] + history,
        )
        self._console = lambda _: None
        self._print_context_preview = lambda _: None
        self._build_messages = lambda history: [{"role": "system", "content": "hello"}] + history


def test_context_runtime_compaction_and_step_projection():
    host = FakeContextHost(True)
    trace = Trace()
    runtime = ContextRuntime(host)
    runtime.maybe_compress_history("pending", trace, 2)
    msgs, base = runtime.build_step_messages(trace, 2)
    assert msgs is base and len(msgs) == 2
    assert [event[0] for event in trace.logs] == [
        "history_compression_triggered", "history_compression_completed",
        "history_compression_final_context", "context_build",
    ]
    assert trace.logs[0][2]["threshold"] == 80


def test_context_runtime_no_compaction_when_under_threshold():
    host = FakeContextHost(False)
    trace = Trace()
    ContextRuntime(host).maybe_compress_history("", trace, 1)
    assert trace.logs == []


def test_runtime_ports_never_import_web_tui_or_code_agent():
    root = Path(__file__).resolve().parents[1]
    for part in ("agent_loop.py", "context_runtime.py"):
        content = (root / "core/runtime" / part).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("agents", "desktop", "tui"))


def test_mcp_tool_recovered_during_runtime_blocks_is_advertised_same_step():
    """Dynamic tool registration must precede each model invocation's schema."""
    class RecoveringHost(Host):
        def __init__(self):
            super().__init__([result("done")])
            self.available = ["Read"]
            self.received_tools = None

        def _collect_runtime_blocks(self, step):
            super()._collect_runtime_blocks(step)
            self.available.append("mcp__recovered__lookup")

        def _get_openai_tools_for_current_mode(self):
            self.actions.append(("schema", len(self.available)))
            return [{"function": {"name": name}} for name in self.available]

        def _invoke_llm_with_retry(
            self, messages, base_messages, tools_schema, tool_choice,
            show_raw, trace_logger, step,
        ):
            self.received_tools = [item["function"]["name"] for item in tools_schema]
            return next(self.results)

    host = RecoveringHost()
    assert AgentLoop(host).run("input", False, Trace()) == "done"
    assert host.received_tools == ["Read", "mcp__recovered__lookup"]
    assert host.actions.index(("runtime", 1)) < host.actions.index(("schema", 2))


def test_context_budget_accounts_for_dynamic_tool_schema():
    from core.context_engine.token_budget import estimate_request_tokens
    messages = [{"role": "system", "content": "Short prompt"}]
    ordinary = estimate_request_tokens(messages, [])
    with_tools = estimate_request_tokens(messages, [
        {"type": "function", "function": {"name": "huge",
          "description": "x" * 15000, "parameters": {"type": "object"}}}
    ])
    assert ordinary > 0 and with_tools > ordinary + 4000


def test_loop_passes_current_tool_catalog_to_compaction_boundary():
    class CaptureHost(Host):
        def _maybe_compress_history(self, pending_input, trace, step, tools_schema):
            assert tools_schema == [{"function": {"name": "Read"}}]
            self.actions.append(("context", step))
    host = CaptureHost([result("done")])
    assert AgentLoop(host).run("input", False, Trace()) == "done"
