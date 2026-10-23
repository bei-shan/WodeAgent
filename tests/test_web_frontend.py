"""Opt-in SPA hosting: the desktop shell loads one loopback URL."""
from __future__ import annotations

from fastapi.testclient import TestClient

from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path)))


def _build_frontend(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Ridge</title>", encoding="utf-8")
    return dist


def test_api_only_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_SERVE_FRONTEND", raising=False)
    _build_frontend(tmp_path)
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/health").status_code == 200


def test_frontend_is_served_when_enabled(tmp_path, monkeypatch):
    dist = _build_frontend(tmp_path)
    monkeypatch.setenv("WEB_SERVE_FRONTEND", "1")
    monkeypatch.setenv("WEB_FRONTEND_DIR", str(dist))

    with _client(tmp_path, monkeypatch) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "Ridge" in root.text
        # API routes still win over the catch-all mount.
        assert client.get("/api/health").status_code == 200
        assert client.get("/does-not-exist").status_code == 404


def test_missing_build_does_not_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SERVE_FRONTEND", "1")
    monkeypatch.setenv("WEB_FRONTEND_DIR", str(tmp_path / "nope"))

    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/health").status_code == 200
