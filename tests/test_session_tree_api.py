"""Real FastAPI session tree/fork integration with durable cursor semantics."""
from __future__ import annotations

from fastapi.testclient import TestClient

from core.context_engine.history_manager import HistoryManager
from core.session_manager import SessionManager
from core.session_store import build_session_snapshot
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


class TreeAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.history_manager = HistoryManager()

    def _build_snapshot(self):
        hm = self.history_manager
        return build_session_snapshot(
            system_messages=[],
            history_messages=hm.serialize_messages(),
            tool_schema=[],
            project_root=self.project_root,
            schema_version=2,
            history_entries=hm.serialize_entries(),
            cursor_id=hm.get_cursor_id(),
            labels=hm._labels,
        )


def test_http_tree_fork_is_durable_and_history_uses_selected_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)

    with TestClient(create_app(agent_factory=TreeAgent, project_root=str(tmp_path))) as client:
        sid = client.post("/api/sessions", json={"title": "branching"}).json()["id"]
        live = client.app.state.controller.get_session(sid)
        agent = live.ensure_agent()
        root = agent.history_manager.append_user("task")
        abandoned = agent.history_manager.append_assistant("abandoned sibling")
        agent._session_manager.save_session(sid, agent._build_snapshot())

        tree = client.get(f"/api/sessions/{sid}/tree")
        assert tree.status_code == 200
        assert root.message_id in tree.json()["nodes"]
        assert tree.json()["cursor_id"] == abandoned.message_id

        denied = client.post(f"/api/sessions/{sid}/fork", json={"target_id": "00000000"})
        assert denied.status_code == 404
        bad = client.post(f"/api/sessions/{sid}/fork", json={"target_id": "xyz"})
        assert bad.status_code == 422
        live._busy = True
        try:
            assert client.post(
                f"/api/sessions/{sid}/fork", json={"target_id": root.message_id},
            ).status_code == 409
        finally:
            live._busy = False

        fork = client.post(f"/api/sessions/{sid}/fork", json={"target_id": root.message_id})
        assert fork.status_code == 200
        assert fork.json() == {"cursor_id": root.message_id}
        saved = SessionManager().load_session(sid)
        assert saved["cursor_id"] == root.message_id
        assert len(saved["history_entries"]) > 2
        assert client.get(f"/api/sessions/{sid}/tree").json()["cursor_id"] == root.message_id
        assert client.get(f"/api/sessions/{sid}/history").json()["messages"] == [
            {"role": "user", "content": "task"},
        ]

    # A server restart must preserve the cursor and every original tree node.
    with TestClient(create_app(agent_factory=TreeAgent, project_root=str(tmp_path))) as client:
        assert client.get(f"/api/sessions/{sid}/tree").json()["cursor_id"] == root.message_id
        assert abandoned.message_id in client.get(f"/api/sessions/{sid}/tree").json()["nodes"]
        assert client.get(f"/api/sessions/{sid}/history").json()["messages"] == [
            {"role": "user", "content": "task"},
        ]
        agent = client.app.state.controller.get_session(sid).agent
        next_msg = agent.history_manager.append_assistant("new continuation")
        assert next_msg.parent_id == root.message_id


def test_unknown_tree_or_fork_sessions_are_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    with TestClient(create_app(agent_factory=TreeAgent, project_root=str(tmp_path))) as client:
        assert client.get("/api/sessions/ffffffffffff/tree").status_code == 404
        # Unknown sessions are hidden before validating a message cursor.
        assert client.post("/api/sessions/ffffffffffff/fork", json={
            "target_id": "abcdefgh",
        }).status_code == 404
        assert client.post("/api/sessions/ffffffffffff/fork", json={
            "target_id": "12345678",
        }).status_code == 404
