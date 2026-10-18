"""Settings and Agent Teams use-case regressions without provider/network calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.runtime.session_controller import SessionController
from core.session_manager import SessionManager
from desktop.service.app import create_app
from desktop.service.application.errors import InvalidInputError, NotFoundError
from desktop.service.application.settings import TeamsService
from tests.test_web_service import FakeAgent


def test_team_creation_names_the_missing_config_not_a_missing_session(tmp_path):
    """A real session whose Agent is not built yet is not a missing session."""
    ctrl = SessionController(workspace_base=str(tmp_path / "workspaces"))
    sid = SessionManager().create_session()
    ctrl.attach_session(sid, FakeAgent)
    service = TeamsService(ctrl)

    with pytest.raises(InvalidInputError, match="not enabled"):
        service.create(sid, "probe-team")

    with pytest.raises(NotFoundError):
        service.create("0123456789ab", "probe-team")


def test_ensure_agent_is_initialized_exactly_once_across_parallel_requests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sid = SessionManager().create_session()
    ctrl = SessionController(workspace_base=str(tmp_path / "workspaces"))
    count = 0

    def factory():
        nonlocal count
        count += 1
        return FakeAgent()

    session = ctrl.attach_session(sid, factory)
    with ThreadPoolExecutor(max_workers=8) as workers:
        instances = list(workers.map(lambda _: session.ensure_agent(), range(16)))
    assert count == 1
    assert all(instance is instances[0] for instance in instances)
    assert session.agent is instances[0]
    ctrl.delete_session(sid)


def test_settings_routes_preserve_contract_and_busy_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.get(f"/api/sessions/{sid}/config").json()["model"] == "fake-model"
        assert client.get(f"/api/sessions/{sid}/teams").json() == []
        assert client.post(f"/api/sessions/{sid}/teams", json={"team_name": "x"}).status_code == 400
        assert client.put(f"/api/sessions/{sid}/config", json={"model_profile": "unknown"}).status_code == 400
        assert client.put(f"/api/sessions/{sid}/config", json={"model_profile": "default"}).status_code == 200
        active = app.state.controller.get_session(sid)
        active._busy = True
        try:
            assert client.put(f"/api/sessions/{sid}/config", json={"plan_mode": True}).status_code == 409
        finally:
            active._busy = False
        assert client.get("/api/sessions/ffffffffffff/config").status_code == 404


def test_enable_team_uses_feature_constructor_and_bootstrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    providers = {}
    initializer = []
    registered = []

    class Bootstrap:
        def provide(self, key, value):
            providers[key] = value

    class TeamAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self._tool_bootstrap = Bootstrap()

    class FakeTeamManager:
        def __init__(self, **kwargs):
            initializer.append(kwargs)
        def export_state(self):
            return {"teams": {"myteam": {
                "queued": 1, "running": 0, "succeeded": 2, "failed": 0,
                "active": 0, "idle": 1, "approvals_pending": 0, "blocked": 0,
            }}}
        def create_team(self, name):
            return {"name": name}
        def shutdown(self):
            pass

    def register(bootstrap):
        registered.append(bootstrap)
        return ["TeamCreate"]

    monkeypatch.setattr("core.team_engine.manager.TeamManager", FakeTeamManager)
    monkeypatch.setattr("core.tool_bootstrap.register_team_tools", register)

    app = create_app(agent_factory=TeamAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        resp = client.put(f"/api/sessions/{sid}/config", json={"enable_agent_teams": True})
        assert resp.status_code == 200
        assert app.state.controller.get_session(sid).agent.enable_agent_teams is True
        assert len(initializer) == 1 and len(registered) == 1
        assert initializer[0]["project_root"].endswith(sid)
        assert initializer[0]["llm"].model == "fake-model"
        assert providers["team_manager"] is app.state.controller.get_session(sid).agent.team_manager
        assert providers["event_sink"] is app.state.controller.get_session(sid).agent.event_sink
        team = client.post(f"/api/sessions/{sid}/teams", json={"team_name": "myteam"})
        assert team.status_code == 201 and team.json()["team"] == "myteam"
        listed = client.get(f"/api/sessions/{sid}/teams").json()
        assert listed[0]["name"] == "myteam" and listed[0]["succeeded"] == 2
        assert client.put(f"/api/sessions/{sid}/config", json={"enable_agent_teams": False}).status_code == 200
        assert client.get(f"/api/sessions/{sid}/teams").json() == []
