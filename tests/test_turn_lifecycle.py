"""Canonical turn lifecycle, status persistence and safe API envelopes."""
import time

from fastapi.testclient import TestClient

from core.llm_recovery import ModelInvocationError
from core.runtime.turn_state import TurnStatus, failure_outcome, load_turn_outcome
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


class CancelledAgent(FakeAgent):
    def run(self, content):
        self.last_turn_status = "cancelled"
        return "已停止生成（用户中断）"


class RateLimitedAgent(FakeAgent):
    def run(self, content):
        raise ModelInvocationError("rate_limited", retryable=True)


class BrokenAgent(FakeAgent):
    def run(self, content):
        raise RuntimeError("secret=private-token should never leave the server")


def _finish(client, sid):
    deadline = time.monotonic() + 3
    while client.get(f"/api/sessions/{sid}").json()["busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not client.get(f"/api/sessions/{sid}").json()["busy"]
    return client.get(f"/api/sessions/{sid}/turn").json()


def test_success_outcome_with_durable_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.get(f"/api/sessions/{sid}/turn").json() == {"status": "idle"}
        assert client.post(f"/api/sessions/{sid}/messages", json={"content": "ping"}).status_code == 200
        state = _finish(client, sid)
        assert state == {
            "status": "completed", "response": "回答：ping",
            "error_kind": None, "retryable": False,
        }
        assert load_turn_outcome(sid).status is TurnStatus.COMPLETED
        events = [x.event for x in app.state.controller.get_session(sid).events.read_after(0).events]
        assert len([e for e in events if e.type == "turn.finished"]) == 1

    app2 = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app2) as client:
        assert client.get(f"/api/sessions/{sid}/turn").json() == state
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert load_turn_outcome(sid) is None


def test_cancelled_turn_is_not_reported_as_completed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=CancelledAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/messages", json={"content": "cancel"})
        state = _finish(client, sid)
        assert state["status"] == "cancelled"
        assert "已停止生成" in state["response"]
        events = [x.event for x in app.state.controller.get_session(sid).events.read_after(0).events]
        terminal = [x for x in events if x.type == "turn.finished"]
        assert len(terminal) == 1 and terminal[0].payload["status"] == "cancelled"


def test_model_failure_reports_category_not_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=RateLimitedAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/messages", json={"content": "go"})
        state = _finish(client, sid)
        assert state["status"] == "failed"
        assert state["error_kind"] == "rate_limited"
        assert state["retryable"] is True
        assert state["response"] == ""
        stream = [x.event for x in app.state.controller.get_session(sid).events.read_after(0).events]
        terminal = [x for x in stream if x.type == "turn.finished"]
        assert len(terminal) == 1 and terminal[0].payload["error_kind"] == "rate_limited"


def test_unexpected_errors_never_expose_exception_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=BrokenAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/messages", json={"content": "go"})
        state = _finish(client, sid)
        assert state["status"] == "failed"
        assert state["error_kind"] == "agent_error"
        assert "secret" not in str(state)
        terminal = [x.event for x in app.state.controller.get_session(sid).events.read_after(0).events if x.event.type == "turn.finished"]
        assert "secret" not in str(terminal[0].payload)
