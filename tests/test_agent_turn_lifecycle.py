"""AgentTurnLifecycle owns status and end events even for failing turns."""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agents.codeAgent import CodeAgent
from core.events import EventType
from core.runtime.turn_lifecycle import AgentTurnLifecycle


class FakeHistory:
    def __init__(self):
        self.users = []
    def append_user(self, content):
        self.users.append(content)
    def get_message_count(self):
        return len(self.users)
    def get_rounds_count(self):
        return len(self.users)


class FakeTrace:
    def __init__(self):
        self.events = []
    def log_event(self, type, payload, step):
        self.events.append((type, payload, step))


class FakeHost:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.logger = logging.getLogger("turn-life-test")
        self.console_verbose = False
        self.console_progress = False
        self.context_builder = SimpleNamespace(set_skills_prompt=lambda _: None)
        self._skills_prompt = ""
        self.trace_logger = FakeTrace()
        self.history_manager = FakeHistory()
        self.events = []
        self.event_sink = SimpleNamespace(emit=self.events.append)
        self._run_id = 0
        self._run_limit_hit = False
        self._interrupt_requested = Event()
        self.save_count = 0
        self.logs = []

    def _refresh_skills_prompt(self): pass
    def _log_system_messages_if_needed(self, trace): pass
    def _log_message_write(self, trace, role, content, metadata, step): pass
    def _auto_save_session(self): self.save_count += 1
    def _console(self, message): self.logs.append(message)
    def _react_loop(self, pending_input, show_raw, trace_logger):
        if self.mode == "error": raise RuntimeError("failure")
        if self.mode == "cancel": self._interrupt_requested.set()
        if self.mode == "limit": self._run_limit_hit = True
        return "done:" + pending_input


@pytest.mark.parametrize("mode,expected", [
    ("ok", "completed"), ("cancel", "cancelled"), ("limit", "failed"),
])
def test_turn_always_persists_and_emits_terminal_event(mode, expected):
    host = FakeHost(mode)
    assert AgentTurnLifecycle(host).run("hello") == "done:hello"
    assert host.history_manager.users == ["hello"]
    assert host.last_turn_status == expected
    assert host.last_turn_error_kind == ("step_limit" if mode == "limit" else None)
    assert host.save_count == 1
    assert not host._interrupt_requested.is_set()
    assert [event.type for event in host.events] == [
        EventType.RUN_STARTED, EventType.RUN_FINISHED,
    ]
    assert host.events[-1].payload["status"] == expected
    assert [kind for kind, _, _ in host.trace_logger.events] == [
        "run_start", "user_input", "run_end",
    ]


def test_exception_preserves_failure_state_and_finish_event():
    host = FakeHost("error")
    with pytest.raises(RuntimeError, match="failure"):
        AgentTurnLifecycle(host).run("hello")
    assert host.last_turn_status == "failed"
    assert host.save_count == 1
    assert host.events[-1].type == EventType.RUN_FINISHED
    assert host.events[-1].payload["status"] == "failed"
    assert host.trace_logger.events[-1][0] == "run_end"


def test_codeagent_compatibility_adapter(monkeypatch):
    monkeypatch.setattr(AgentTurnLifecycle, "run",
                        lambda self, content, **kwargs: "forwarded:" + content)
    agent = CodeAgent.__new__(CodeAgent)
    assert agent._run_scoped("hello") == "forwarded:hello"


def test_engine_lifecycle_has_no_ui_or_codeagent_imports():
    root = Path(__file__).resolve().parents[1]
    source = (root / "core/runtime/turn_lifecycle.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("desktop", "tui", "agents", "fastapi"))
