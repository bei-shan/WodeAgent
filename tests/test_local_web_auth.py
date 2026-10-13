"""Opt-in local operator authentication and CSRF/WS admission tests.

This is intentionally single-user only. Owner-bound RBAC is separate work.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from desktop.service.local_auth import COOKIE_NAME, COOKIE_TTL, LocalWebAuth
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent
from core.events import AgentEvent

TOKEN = "x" * 48
ORIGIN = {"origin": "http://localhost:5173"}


def build_client(tmp_path, monkeypatch, secure=True):
    monkeypatch.chdir(tmp_path)
    if secure:
        monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    else:
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    return app, TestClient(app, base_url="http://localhost:8000")


def login(client):
    return client.post("/api/auth/login", json={"token": TOKEN}, headers=ORIGIN)


def test_unprotected_local_dev_retains_legacy_behavior(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch, secure=False)
    with client:
        assert client.get("/api/auth/status").json() == {
            "required": False, "authenticated": True,
        }
        assert client.get("/api/sessions").status_code == 200


def test_auth_gates_api_and_docs_before_login(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/auth/status").json() == {
            "required": True, "authenticated": False,
        }
        assert client.get("/api/sessions").status_code == 401
        assert client.get("/api/health").status_code == 401
        assert client.get("/docs").status_code == 401
        assert client.post("/api/sessions", json={}, headers=ORIGIN).status_code == 401
        assert client.post("/api/auth/login", json={"token": "bad"}, headers=ORIGIN).status_code == 401


def test_cookie_secure_flags_expiry_and_rotation(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    with client:
        response = login(client)
        assert response.status_code == 200
        cookie_header = response.headers["set-cookie"].lower()
        assert "httponly" in cookie_header
        assert "samesite=strict" in cookie_header
        assert "max-age=" in cookie_header
        assert COOKIE_NAME in client.cookies
        assert client.get("/api/auth/status").json()["authenticated"] is True
        assert client.get("/api/sessions").status_code == 200

        forged = COOKIE_NAME + "=tampered"
        with TestClient(client.app, base_url="http://localhost:8000") as attacker:
            assert attacker.get("/api/sessions", headers={"cookie": forged}).status_code == 401
        assert client.post("/api/auth/logout", headers=ORIGIN).status_code == 200
        assert client.get("/api/auth/status").json()["authenticated"] is False


def test_csrf_and_invalid_origin_block_cookie_mutations(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    with client:
        assert login(client).status_code == 200
        assert client.post("/api/sessions", json={}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/sessions", json={}, headers=ORIGIN).status_code == 201


def test_nonlocal_host_and_login_origin_are_rejected(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/auth/status", headers={"host": "evil.example"}).status_code == 403
        assert client.post("/api/auth/login", json={"token": TOKEN}).status_code == 403
        assert client.post("/api/auth/login", json={"token": TOKEN}, headers={
            "origin": "https://evil.example"
        }).status_code == 403


def test_bearer_for_explicit_api_clients_but_not_cookie_csrf_bypass(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/sessions", headers={"authorization": "Bearer " + TOKEN}).status_code == 200
        assert client.post(
            "/api/sessions", json={}, headers={"authorization": "Bearer " + TOKEN}
        ).status_code == 201
        assert client.get("/api/sessions", headers={"authorization": "Bearer wrong"}).status_code == 401


def test_login_attempts_are_rate_limited(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    with client:
        for i in range(5):
            assert client.post("/api/auth/login", json={"token": "bad"}, headers=ORIGIN).status_code == 401
        assert login(client).status_code == 429


def test_ws_requires_cookie_and_trusted_origin(tmp_path, monkeypatch):
    app, client = build_client(tmp_path, monkeypatch)
    with client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/sessions/some-session/stream", headers={**ORIGIN, "host": "localhost:8000"}):
                pass

        assert login(client).status_code == 200
        sid = client.post("/api/sessions", json={}, headers=ORIGIN).json()["id"]
        session = app.state.controller.get_session(sid)
        session.events.put(AgentEvent("step.started", {"step": 1}, step=1))
        # TestClient does not reliably propagate its HTTP cookie jar through
        # WebSocketTestSession. A real browser sends this Cookie header.
        ws_headers = {
            **ORIGIN,
            "host": "localhost:8000",
            "cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}",
        }

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/api/sessions/{sid}/stream?after=0",
                headers={**ws_headers, "origin": "https://evil.example"},
            ):
                pass

        with client.websocket_connect(
            f"/api/sessions/{sid}/stream?after=0", headers=ws_headers
        ) as ws:
            frame = ws.receive_json()
            assert frame["type"] == "step.started"
            assert frame["seq"] == 1


def test_expired_and_modified_cookie_are_rejected(monkeypatch):
    local = LocalWebAuth(TOKEN)
    cookie = local.issue_cookie()
    assert local.validate_cookie(cookie)
    monkeypatch.setattr("desktop.service.local_auth.time.time", lambda: 1e12)
    assert not local.validate_cookie(cookie)
    assert not local.validate_cookie(cookie + "x")


def test_short_credential_refused_at_app_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_AUTH_TOKEN", "short")
    with pytest.raises(ValueError, match="24"):
        create_app(agent_factory=FakeAgent, project_root=str(tmp_path))

def test_short_numeric_hex_prefix_never_aliases_a_list_index(tmp_path):
    from core.session_manager import SessionManager
    mgr = SessionManager(sessions_dir=str(tmp_path))
    sid = mgr.create_session()
    assert mgr.resolve_identifier("01") is None
    assert mgr.resolve_identifier("1") == sid



def test_terminal_bearer_websocket_and_rest_share_credentials(tmp_path, monkeypatch):
    """Bun TUI sends Authorization headers; no browser cookie/Origin is required."""
    app, client = build_client(tmp_path, monkeypatch)
    bearer = {"host": "localhost:8000", "authorization": "Bearer " + TOKEN}
    with client:
        sid = client.post("/api/sessions", json={}, headers=bearer).json()["id"]
        assert client.get("/api/sessions", headers=bearer).status_code == 200
        session = app.state.controller.get_session(sid)
        session.events.put(AgentEvent("step.started", {"step": 1}, step=1))
        with client.websocket_connect(
            f"/api/sessions/{sid}/stream?after=0",
            headers=bearer,
        ) as ws:
            event = ws.receive_json()
            assert event["type"] == "step.started"
            assert event["seq"] == 1

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/api/sessions/{sid}/stream?after=0",
                headers={**bearer, "authorization": "Bearer wrong"},
            ):
                pass
