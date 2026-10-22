"""Application services: API compatibility, lifecycle concurrency and boundaries."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from core.runtime.session_controller import SessionController
from core.session_manager import SessionManager
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def test_concurrent_resume_same_id_without_temporary_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = SessionManager()
    sid = catalog.create_session("Persisted")
    ctrl = SessionController(workspace_base=str(tmp_path / "workspaces"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = list(pool.map(
            lambda _: ctrl.attach_session(sid, FakeAgent, title="Persisted"),
            range(16),
        ))
    assert all(session is sessions[0] for session in sessions)
    assert ctrl.list_sessions() == [sid]
    assert sessions[0].session_id == sid
    assert [item.id for item in catalog.list_sessions()] == [sid]
    assert ctrl.get_session(sid).title == "Persisted"


def test_http_contract_and_persistent_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"title": "Persist me"})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert SessionManager().get_session(sid).title == "Persist me"
        assert any(item["id"] == sid and item["title"] == "Persist me" for item in client.get("/api/sessions").json())
        assert client.post(f"/api/sessions/{sid}/pin").json() == {"pinned": True}
        assert client.get(f"/api/sessions/{sid}").json()["pinned"] is True
        assert client.post(f"/api/sessions/{sid}/pin").json() == {"pinned": False}
        assert client.put(f"/api/sessions/{sid}/rename", json={"title": "  "}).status_code == 400
        assert client.put(f"/api/sessions/{sid}/rename", json={"title": "Renamed"}).status_code == 200
        assert SessionManager().get_session(sid).title == "Renamed"
        assert client.get("/api/sessions/ffffffffffff").status_code == 404
        assert client.get("/api/sessions/ffffffffffff/turn").status_code == 404
        assert client.post("/api/sessions/ffffffffffff/messages", json={"content": "hello"}).status_code == 404
        assert client.post(f"/api/sessions/{sid}/permissions/missing/resolve", json={"decision": "denied"}).status_code == 404
        assert client.post(f"/api/sessions/{sid}/ask-user/missing/answer", json={"answer": "no"}).status_code == 404
        workspace = tmp_path / ".mycodeagent" / "sessions" / sid
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "test.txt").write_text("hello")
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert not workspace.exists()
        assert SessionManager().get_session(sid) is None
        assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_process_restart_reuses_record_and_has_no_ephemeral_catalog_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    with TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path))) as client:
        sid = client.post("/api/sessions", json={"title": "Original"}).json()["id"]
    before = {info.id for info in SessionManager().list_sessions()}
    with TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path))) as client:
        assert client.post(f"/api/sessions/{sid}/activate").json()["status"] == "activated"
        assert client.post(f"/api/sessions/{sid}/activate").json()["status"] == "active"
        assert client.get(f"/api/sessions/{sid}").json()["title"] == "Original"
        assert {info.id for info in SessionManager().list_sessions()} == before


def test_stale_index_entry_is_hidden_and_pruned_on_activate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    catalog = SessionManager()
    ghost = catalog.create_session("Ghost without snapshot")
    # Simulate a catalog entry whose snapshot was removed outside the catalog.
    catalog._snapshot_path(ghost).unlink()
    assert catalog.get_session(ghost) is not None
    assert not catalog.snapshot_exists(ghost)
    with TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path))) as client:
        listed = {item["id"] for item in client.get("/api/sessions").json()}
        assert ghost not in listed
        response = client.post(f"/api/sessions/{ghost}/activate")
        assert response.status_code == 404
        assert "no longer exists" in response.json()["detail"]
    # The unusable entry is gone so it cannot be offered again.
    assert SessionManager().get_session(ghost) is None


def test_architecture_boundaries():
    root = Path(__file__).resolve().parents[1]
    application = root / "desktop" / "service" / "application"
    for file in application.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert all(not name.startswith(("fastapi", "starlette", "tui", "desktop.web")) for name in imports), (file.name, imports)
    app = (root / "desktop" / "service" / "app.py").read_text(encoding="utf-8")
    assert "ctrl._sessions" not in app
    assert "session.session_id = sid" not in app
