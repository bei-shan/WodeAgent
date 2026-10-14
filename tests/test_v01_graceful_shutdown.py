"""v0.1 shutdown acceptance: drain worker threads before closing resources."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.runtime.session_controller import (
    SessionController, _SessionPermissionBroker, AgentSession,
)
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


class CooperativeAgent:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.close_count = 0
        self.interrupt_count = 0

    def run(self, _content):
        self.started.set()
        self.stopped.wait(timeout=4)
        self.last_turn_status = "cancelled"
        return "stopped"

    def interrupt(self):
        self.interrupt_count += 1
        self.stopped.set()

    def close(self):
        self.close_count += 1


class UncooperativeAgent(CooperativeAgent):
    def __init__(self):
        super().__init__()
        self.allow_finish = threading.Event()

    def run(self, _content):
        self.started.set()
        self.allow_finish.wait(timeout=4)
        self.last_turn_status = "cancelled"
        return "late"

    def interrupt(self):
        self.interrupt_count += 1  # deliberately ignores the interruption


def _live_controller():
    ctrl = SessionController()
    sid = ctrl.create_session(lambda: None)
    session = ctrl.get_session(sid)
    assert session is not None
    return ctrl, sid, session


def test_shutdown_interrupts_and_joins_cooperative_worker_before_close(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctrl, sid, session = _live_controller()
    agent = CooperativeAgent()
    session._agent = agent
    assert session.send_message("task")
    assert agent.started.wait(2)
    outcome = ctrl.shutdown(grace_seconds=1.0)
    assert outcome == {"closed": 1, "running": 0}
    assert agent.interrupt_count == 1
    assert agent.close_count == 1
    assert ctrl.list_sessions() == []
    assert session.last_turn.status.value == "cancelled"
    assert session.send_message("stale") is False
    assert [entry.event.type for entry in session.events.read_after(0).events][-2:] == [
        "turn.finished", "turn.completed",
    ]
    with pytest.raises(RuntimeError, match="shutting down"):
        ctrl.create_session(lambda: None)
    with pytest.raises(RuntimeError, match="shutting down"):
        ctrl.attach_session(sid, lambda: None)


def test_shutdown_does_not_close_live_uncooperative_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctrl, sid, session = _live_controller()
    agent = UncooperativeAgent()
    session._agent = agent
    assert session.send_message("slow")
    assert agent.started.wait(2)
    started = time.monotonic()
    outcome = ctrl.shutdown(grace_seconds=0.03)
    assert time.monotonic() - started < 1
    assert outcome == {"closed": 0, "running": 1}
    assert agent.interrupt_count == 1
    assert agent.close_count == 0  # no teardown while worker executes
    assert ctrl.get_session(sid) is session
    assert session.send_message("more") is False
    agent.allow_finish.set()
    session._thread.join(timeout=2)
    assert not session._thread.is_alive()
    outcome = ctrl.shutdown(grace_seconds=0.1)
    assert outcome == {"closed": 1, "running": 0}
    assert agent.close_count == 1


def test_shutdown_wakes_pending_permission_prompt_without_waiting_120s(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctrl, _, session = _live_controller()

    class PermissionAgent(CooperativeAgent):
        def run(self, _content):
            self.started.set()
            decision = _SessionPermissionBroker(session)("/workspace", "Write", "write")
            self.last_turn_status = "cancelled"
            return decision

        def interrupt(self):
            self.interrupt_count += 1

    agent = PermissionAgent()
    session._agent = agent
    assert session.send_message("request write")
    assert agent.started.wait(2)
    until = time.monotonic() + 2
    while not session.pending_prompts() and time.monotonic() < until:
        time.sleep(0.005)
    assert len(session.pending_prompts()) == 1
    outcome = ctrl.shutdown(grace_seconds=1)
    assert outcome == {"closed": 1, "running": 0}
    assert session.last_turn.response == "denied"
    assert not session.pending_prompts()
    assert agent.close_count == 1


def test_web_app_shutdown_invokes_bounded_controller_drain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    called = []
    original = app.state.controller.shutdown

    def wrapper(grace_seconds=2):
        called.append(grace_seconds)
        return original(grace_seconds=grace_seconds)

    app.state.controller.shutdown = wrapper
    with TestClient(app) as client:
        result = client.post("/api/sessions", json={})
        assert result.status_code == 201 or result.status_code == 200
        sid = result.json()["id"]
        assert client.get(f"/api/sessions/{sid}").status_code == 200
    assert called == [2.0]
    assert app.state.controller.list_sessions() == []


def test_shutdown_validates_grace_period(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctrl = SessionController()
    for bad in (-0.01, 30.1):
        with pytest.raises(ValueError):
            ctrl.shutdown(grace_seconds=bad)


def test_bad_interrupt_hook_still_denies_pending_permission():
    session = AgentSession("0123456789ab", lambda: None)
    class Broken:
        def interrupt(self):
            raise RuntimeError("untrusted interrupt hook error")

    session._agent = Broken()
    session._busy = True
    answer = []
    worker = threading.Thread(
        target=lambda: answer.append(
            _SessionPermissionBroker(session)("/sensitive", "Write", "write")
        )
    )
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while not session.pending_prompts() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert session.pending_prompts()
        with pytest.raises(RuntimeError, match="interrupt hook"):
            session.interrupt()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert answer == ["denied"]
        assert session.pending_prompts() == []
    finally:
        session._interaction_cancelled.set()
        session._release_interaction_waiters()
        worker.join(timeout=1)
