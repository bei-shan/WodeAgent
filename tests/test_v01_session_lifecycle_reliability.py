"""v0.1 regression: interrupt immediately wakes interactive waits and deletion is safe."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.runtime.session_controller import (
    AgentSession, _SessionPermissionBroker, _SessionAskUserFunc,
)
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _pending(session: AgentSession, *, kind: str) -> bool:
    if kind == "permission":
        return bool(session._pending_permissions)
    return bool(session._pending_ask_user)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("permission", "denied"), ("ask_user", "")],
)
def test_interrupt_wakes_live_prompt_and_prevents_late_resolution(kind, expected):
    session = AgentSession("123456789abc", lambda: None)
    called = []
    session._agent = SimpleNamespace(interrupt=lambda: called.append("interrupt"))
    session._busy = True
    result = []
    broker = (_SessionPermissionBroker(session) if kind == "permission"
              else _SessionAskUserFunc(session))
    def wait_for_answer():
        result.append(broker("/outside", "Read", "read") if kind == "permission"
                      else broker("Sensitive question"))
    worker = threading.Thread(target=wait_for_answer)
    worker.start()
    deadline = time.monotonic() + 2
    while not _pending(session, kind=kind) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert _pending(session, kind=kind), "Prompt never reached the pending registry"
    if kind == "permission":
        request_id = next(iter(session._pending_permissions))
    else:
        request_id = next(iter(session._pending_ask_user))
    try:
        assert session.interrupt() is True
        worker.join(timeout=1)
        assert not worker.is_alive(), "Interactive request waited for its normal 120/300s timeout"
        assert result == [expected]
        assert session.pending_prompts() == []
        assert called == ["interrupt"]
        if kind == "permission":
            assert not session.resolve_permission(request_id, "granted")
            assert broker("/outside", "Read", "read") == "denied"
        else:
            assert not session.answer_ask_user(request_id, "late answer")
            assert broker("another question") == ""
        assert not _pending(session, kind=kind)
    finally:
        session._interaction_cancelled.set()
        session._release_interaction_waiters()
        worker.join(timeout=1)


def test_interrupt_idle_session_is_rejected_and_next_turn_resets_signal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = AgentSession("123456789abc", lambda: None)
    calls = []
    session._agent = SimpleNamespace(interrupt=lambda: calls.append("stop"),
                                     run=lambda content: "done")
    assert session.interrupt() is False
    assert not session._interaction_cancelled.is_set()
    session._busy = True
    assert session.interrupt() is True
    assert session._interaction_cancelled.is_set()
    session._busy = False
    assert session.send_message("next") is True
    session._thread.join(timeout=2)
    assert not session._interaction_cancelled.is_set()
    assert session.last_turn.status.value == "completed"


def test_prompt_request_after_cancel_never_publishes_stale_prompt():
    session = AgentSession("123456789abc", lambda: None)
    session._agent = SimpleNamespace(interrupt=lambda: None)
    session._busy = True
    assert session.interrupt()
    _SessionPermissionBroker(session)("/outside", "Write", "write")
    _SessionAskUserFunc(session)("details")
    assert session.pending_prompts() == []
    assert not session.events.read_after(0).events


class WaitingAgent(FakeAgent):
    started = threading.Event()
    release = threading.Event()
    interrupt_calls = 0

    def run(self, content):
        type(self).started.set()
        type(self).release.wait(timeout=3)
        return super().run(content)

    def interrupt(self):
        type(self).interrupt_calls += 1


def test_http_running_session_delete_conflicts_and_preserves_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    WaitingAgent.started = threading.Event()
    WaitingAgent.release = threading.Event()
    WaitingAgent.interrupt_calls = 0
    app = create_app(agent_factory=WaitingAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        try:
            assert client.post(f"/api/sessions/{sid}/messages",
                               json={"content": "work"}).status_code == 200
            assert WaitingAgent.started.wait(timeout=2)
            assert client.delete(f"/api/sessions/{sid}").status_code == 409
            assert client.get(f"/api/sessions/{sid}").json()["busy"] is True
            assert client.post(f"/api/sessions/{sid}/interrupt").status_code == 200
            assert WaitingAgent.interrupt_calls == 1
            assert client.post(f"/api/sessions/{sid}/messages",
                               json={"content": "racing"}).status_code == 409
        finally:
            WaitingAgent.release.set()
        deadline = time.monotonic() + 3
        while client.get(f"/api/sessions/{sid}").json()["busy"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not client.get(f"/api/sessions/{sid}").json()["busy"]
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_closed_session_reference_cannot_restart():
    session = AgentSession("123456789abc", lambda: None)
    session._agent = SimpleNamespace(run=lambda x: x)
    from core.runtime.session_controller import SessionController
    controller = SessionController()
    controller._sessions[session.session_id] = session
    assert controller.delete_session(session.session_id) is True
    assert session.send_message("stale request") is False
