"""Manual compaction: focus injection, HTTP contract and refusal paths."""
from __future__ import annotations

from fastapi.testclient import TestClient

from core.config import Config
from core.context_engine.history_manager import HistoryManager
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _manager(captured: dict) -> HistoryManager:
    def generator(messages):
        captured["messages"] = list(messages)
        return "compacted summary"

    return HistoryManager(
        config=Config(min_retain_rounds=2), summary_generator=generator,
    )


def test_compact_injects_the_focus_into_the_summary_input():
    captured: dict = {}
    manager = _manager(captured)
    for index in range(5):
        manager.append_user(f"question {index}")
        manager.append_assistant(f"answer {index}")

    info = manager.compact(return_info=True, focus="keep the API design details")
    assert info["compressed"] is True
    assert info["summary_generated"] is True
    texts = [message.content for message in captured["messages"]]
    assert any("keep the API design details" in text for text in texts)
    assert any("Compaction focus requested by the user" in text for text in texts)


def test_compact_without_focus_sends_no_focus_message():
    captured: dict = {}
    manager = _manager(captured)
    for index in range(5):
        manager.append_user(f"question {index}")
        manager.append_assistant(f"answer {index}")

    assert manager.compact(return_info=True)["compressed"] is True
    assert not any(
        "Compaction focus" in message.content for message in captured["messages"]
    )


def _app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return create_app(agent_factory=FakeAgent, project_root=str(tmp_path))


class _FakeHistory:
    def __init__(self):
        self.focus = None

    def compact(self, *, focus=None, return_info=False):
        self.focus = focus
        return {
            "compressed": True, "summary_generated": True,
            "messages_before": 12, "rounds_count": 12,
        }


class _FakeAgent:
    def __init__(self):
        self.history_manager = _FakeHistory()


class _FakeSession:
    def __init__(self, busy=False):
        self.busy = busy
        self.agent = _FakeAgent()

    def ensure_agent(self):
        return self.agent


def test_compact_endpoint_forwards_focus(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "c"}).json()["id"]
        session = _FakeSession()
        app.state.controller.get_session = lambda _sid: session

        response = client.post(f"/api/sessions/{sid}/compact", json={"focus": "keep tests"})
        assert response.status_code == 200
        body = response.json()
        assert body["compacted"] is True
        assert body["summary_generated"] is True
        assert session.agent.history_manager.focus == "keep tests"


def test_compact_endpoint_refuses_busy_sessions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "c"}).json()["id"]
        app.state.controller.get_session = lambda _sid: _FakeSession(busy=True)
        response = client.post(f"/api/sessions/{sid}/compact", json={})
        assert response.status_code == 400
        assert "finish" in response.json()["detail"].lower()


def test_compact_endpoint_rejects_unknown_sessions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/sessions/ffffffffffff/compact", json={}).status_code == 404

def test_compact_appends_a_bounded_path_digest():
    import json

    captured: dict = {}
    manager = _manager(captured)
    for index in range(5):
        manager.append_user(f"question {index}")
        manager.append_assistant("working", metadata={"tool_calls": [
            {"function": {"name": "Read", "arguments": json.dumps({"path": f"src/file{index}.py"})}},
        ]})

    assert manager.compact(return_info=True)["compressed"] is True
    summary = next(m.content for m in manager._messages if m.role == "summary")
    assert "<files_touched>" in summary
    assert "src/file0.py" in summary
    # The retained tail is not part of the compressed region.
    assert "src/file4.py" not in summary
