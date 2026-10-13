"""Web service regression tests without external LLM calls."""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from core.events import AgentEvent, EventType
from core.session_manager import SessionManager
from desktop.service.app import create_app


class FakeHistory:
    def __init__(self):
        self.messages = []
    def load_messages(self, messages):
        self.messages = list(messages)
    def load_entries(self, entries):
        self.messages = [entry for entry in entries if entry.get("role") in ("user", "assistant")]
    def get_thinking_level(self):
        return "off"


class FakeAgent:
    def __init__(self):
        self._session_manager = SessionManager()
        self._session_id = self._session_manager.create_session()
        self.history_manager = FakeHistory()
        self.tool_registry = SimpleNamespace(get_tool=lambda _: None)
        self.llm = SimpleNamespace(model="fake-model", provider="fake")
        self.config = SimpleNamespace(enable_agent_teams=False)
        self.enable_agent_teams = False
        self._in_plan_mode = False
        self.team_manager = None
        self.event_sink = None
        self.llm_stream_callback = None

    def _rebind_project_root(self, root):
        self.project_root = root

    def run(self, content):
        answer = "回答：" + content
        self.history_manager.messages.extend([
            {"role": "user", "content": content},
            {"role": "assistant", "content": answer},
        ])
        self._session_manager.save_session(
            self._session_id,
            {"history_messages": self.history_manager.messages},
        )
        if self.llm_stream_callback:
            self.llm_stream_callback("content", answer, 1)
        self.event_sink.emit(AgentEvent(EventType.ASSISTANT_FINAL, {"content": answer}, step=1))
        return answer

    def close(self):
        pass


def test_web_session_roundtrip_and_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "external-state"))
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        created = client.post("/api/sessions", json={"title": ""}).json()
        sid = created["id"]
        assert len(sid) == 12
        assert client.get(f"/api/sessions/{sid}/files").status_code == 200
        assert client.get(f"/api/sessions/{sid}/files", params={"path": ".."}).status_code == 403

        upload = client.post(
            f"/api/sessions/{sid}/upload",
            files={"file": ("example.txt", b"hello web", "text/plain")},
        )
        assert upload.status_code == 200
        assert upload.json()["path"] == "example.txt"
        assert client.get(
            f"/api/sessions/{sid}/files/content",
            params={"path": "example.txt"},
        ).status_code == 200

        assert client.post(f"/api/sessions/{sid}/messages", json={"content": "你好"}).status_code == 200
        deadline = time.monotonic() + 3
        while client.get(f"/api/sessions/{sid}").json()["busy"] and time.monotonic() < deadline:
            time.sleep(0.01)
        data = client.get(f"/api/sessions/{sid}/history").json()
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
        assert SessionManager().load_session(sid) is not None
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert SessionManager().load_session(sid) is None


def test_config_model_and_runtime_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "state"))
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        config = client.get(f"/api/sessions/{sid}/config")
        assert config.status_code == 200
        assert config.json()["model"] == "fake-model"
        assert config.json()["thinking_level"] == "off"
        assert client.put(f"/api/sessions/{sid}/config", json={"model_profile": "unknown"}).status_code == 400
        assert client.put(f"/api/sessions/{sid}/config", json={"model_profile": "default"}).status_code == 200
        with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
            assert client.post(f"/api/sessions/{sid}/messages", json={"content": "ok"}).status_code == 200
            received = [ws.receive_json()["type"] for _ in range(3)]
            assert "llm.delta" in received
            assert "assistant.final" in received

def test_restart_restores_same_agent_conversation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    app1 = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app1) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/messages", json={"content": "first"})
        deadline = time.monotonic() + 3
        while client.get(f"/api/sessions/{sid}").json()["busy"] and time.monotonic() < deadline:
            time.sleep(0.01)

    # A new Web process activates the same disk session and resumes history.
    app2 = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app2) as client:
        sessions = client.get("/api/sessions").json()
        assert any(s["id"] == sid for s in sessions)
        assert client.post(f"/api/sessions/{sid}/activate").status_code == 200
        assert client.get(f"/api/sessions/{sid}/config").status_code == 200
        client.post(f"/api/sessions/{sid}/messages", json={"content": "second"})
        deadline = time.monotonic() + 3
        while client.get(f"/api/sessions/{sid}").json()["busy"] and time.monotonic() < deadline:
            time.sleep(0.01)
        history = client.get(f"/api/sessions/{sid}/history").json()["messages"]
        assert [m["content"] for m in history] == [
            "first", "回答：first", "second", "回答：second",
        ]

def test_documented_factory_entrypoint_and_missing_session_are_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The documented uvicorn ...:create_app --factory path must work without
    # initializing an LLM or requiring credentials at server startup.
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/sessions/not-a-session/files").status_code == 404
        assert client.get("/api/sessions/not-a-session/files/content", params={"path": "README.md"}).status_code == 404
