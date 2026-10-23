"""Goal HTTP API: read, set and clear a session objective."""
from __future__ import annotations

from fastapi.testclient import TestClient

from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path)))


def _new_session(client) -> str:
    return client.post("/api/sessions", json={"title": "goal"}).json()["id"]


def test_goal_is_null_until_set(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        sid = _new_session(client)
        assert client.get(f"/api/sessions/{sid}/goal").json() == {"goal": None}


def test_goal_can_be_set_read_and_cleared(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        sid = _new_session(client)

        response = client.put(
            f"/api/sessions/{sid}/goal",
            json={"objective": "Ship the migration", "max_rounds": 3},
        )
        assert response.status_code == 200
        goal = response.json()["goal"]
        assert goal["objective"] == "Ship the migration"
        assert goal["max_rounds"] == 3
        assert goal["status"] == "active"

        assert client.get(f"/api/sessions/{sid}/goal").json()["goal"]["objective"] == "Ship the migration"

        assert client.delete(f"/api/sessions/{sid}/goal").json() == {"cleared": True}
        assert client.get(f"/api/sessions/{sid}/goal").json() == {"goal": None}
        assert client.delete(f"/api/sessions/{sid}/goal").json() == {"cleared": False}


def test_goal_matches_the_tool_store_path(tmp_path, monkeypatch):
    from core.goals import GoalStore

    with _client(tmp_path, monkeypatch) as client:
        sid = _new_session(client)
        client.put(f"/api/sessions/{sid}/goal", json={"objective": "shared store"})

    # The Goal tool writes under <project_root>/memory/goals; the API must agree.
    store = GoalStore(tmp_path / "memory" / "goals")
    assert store.get(sid).objective == "shared store"


def test_invalid_goal_inputs_are_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        sid = _new_session(client)
        assert client.put(f"/api/sessions/{sid}/goal", json={"objective": "  "}).status_code == 400
        assert client.put(
            f"/api/sessions/{sid}/goal", json={"objective": "x", "max_rounds": "nope"},
        ).status_code == 400


def test_unknown_session_is_not_found(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/sessions/ffffffffffff/goal").status_code == 404
        assert client.put(
            "/api/sessions/ffffffffffff/goal", json={"objective": "x"},
        ).status_code == 404
