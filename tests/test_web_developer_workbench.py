"""Web developer workbench security and durability regression tests."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from core.events import AgentEvent
from core.runtime.execution_history import ExecutionHistory
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return create_app(agent_factory=FakeAgent, project_root=str(tmp_path))


def test_persisted_tools_redacted_and_deleted(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        session = app.state.controller.get_session(sid)
        session.events.put(AgentEvent("tool.started", {
            "tool_call_id": "c-1", "tool": "Bash",
            "input": {"command": "echo SECRET PASSWORD"},
        }, step=3))
        session.events.put(AgentEvent("tool.completed", {
            "tool_call_id": "c-1", "tool": "Bash", "status": "success",
            "output": json.dumps({"data": {
                "sandbox": "docker", "exit_code": 0,
                "stdout": "sk-" + "x" * 28, "stderr": "",
            }}),
        }, step=3))
        items = client.get(f"/api/sessions/{sid}/execution-history").json()["tools"]
        assert len(items) == 1
        assert items[0]["status"] == "success"
        assert items[0]["input"] == {}
        assert "PASSWORD" not in json.dumps(items)
        assert "sk-***" in items[0]["output"]
        assert "x" * 28 not in items[0]["output"]
    # New service instance reopens same SQLite state, no in-memory dependency.
    app2 = _app(tmp_path, monkeypatch)
    with TestClient(app2) as client:
        history = client.get(f"/api/sessions/{sid}/execution-history")
        assert history.status_code == 200
        assert history.json()["tools"][0]["name"] == "Bash"
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert ExecutionHistory(tmp_path / "state" / "sessions").list(sid) == {"tools": []}


def test_execution_history_bounded(tmp_path):
    store = ExecutionHistory(tmp_path / "state" / "sessions")
    sid = "a" * 12
    for index in range(230):
        store.append(sid, AgentEvent("tool.started", {
            "tool_call_id": f"call-{index}", "tool": "Read", "input": {"api_key": "NEVER"},
        }, step=index))
    rows = store.list(sid)["tools"]
    assert len(rows) == 200
    assert rows[0]["id"] == "call-30"
    assert "NEVER" not in json.dumps(rows)


def test_git_diff_real_head_and_isolation(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        root = tmp_path / "state" / "sessions" / sid
        assert client.get(f"/api/sessions/{sid}/git/diff", params={"path": "a.txt"}).status_code == 404
        root.mkdir(exist_ok=True, parents=True)
        file = root / "a.txt"
        file.write_text("before\n", encoding="utf-8")
        endpoint = f"/api/sessions/{sid}/git/diff"
        if shutil.which("git") is None:
            pytest.skip("Git binary not installed")
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args],
                check=True, capture_output=True, text=True, env={
                    "PATH": __import__("os").environ.get("PATH", ""),
                    "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
                })
        git("init", "-q")
        git("-c", "user.name=Test", "-c", "user.email=t@example.org",
            "add", "a.txt")
        git("-c", "user.name=Test", "-c", "user.email=t@example.org",
            "commit", "-qm", "baseline")
        file.write_text("after\n", encoding="utf-8")
        result = client.get(endpoint, params={"path": "a.txt"})
        assert result.status_code == 200
        body = result.json()
        assert body["available"] is True
        assert "-before" in body["patch"]
        assert "+after" in body["patch"]
        assert client.get(endpoint, params={"path": "../../secret"}).status_code == 403
        assert client.get(endpoint, params={"path": ".git/config"}).status_code == 403
        outside = tmp_path / "outside.txt"
        outside.write_text("PRIVATE")
        (root / "leak.txt").symlink_to(outside)
        assert client.get(endpoint, params={"path": "leak.txt"}).status_code == 403


def test_terminal_fails_closed_without_web_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SANDBOX_TERMINAL_ENABLED", "true")
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.get("/api/sandbox/terminal-capabilities").json()["enabled"] is False
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/sessions/{sid}/terminal") as websocket:
                websocket.receive_text()
