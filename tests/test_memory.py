"""Local memory: settings persistence, service behavior and HTTP surface."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core.memory.service import LocalMemoryService
from core.memory.settings import (
    MemorySettings,
    MemorySettingsError,
    load_settings,
    save_settings,
    settings_path,
)
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return tmp_path


def _enable(tmp_path, provider="sqlite", limit=8) -> None:
    save_settings(tmp_path, MemorySettings(enabled=True, provider=provider, limit=limit))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_settings_default_off_and_roundtrip(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert load_settings(tmp_path).to_dict() == {
        "enabled": False, "provider": "off", "limit": 8,
        "auto_capture": False, "max_per_turn": 1,
    }

    saved = save_settings(tmp_path, MemorySettings(
        enabled=True, provider="sqlite", limit=5, auto_capture=True, max_per_turn=2,
    ))
    assert saved.enabled is True
    assert load_settings(tmp_path) == MemorySettings(True, "sqlite", 5, True, 2)
    assert json.loads(settings_path(tmp_path).read_text(encoding="utf-8"))["provider"] == "sqlite"


def test_settings_validation(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with pytest.raises(MemorySettingsError):
        save_settings(tmp_path, MemorySettings(enabled=True, provider="off"))
    with pytest.raises(MemorySettingsError):
        save_settings(tmp_path, MemorySettings(enabled=True, provider="nope"))
    with pytest.raises(MemorySettingsError):
        save_settings(tmp_path, MemorySettings(enabled=True, provider="sqlite", limit=0))
    # Automatic capture requires memory itself.
    with pytest.raises(MemorySettingsError):
        save_settings(tmp_path, MemorySettings(
            enabled=False, provider="off", auto_capture=True,
        ))
    with pytest.raises(MemorySettingsError):
        save_settings(tmp_path, MemorySettings(
            enabled=True, provider="sqlite", max_per_turn=9,
        ))


def test_corrupt_settings_fall_back_to_off(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    path = settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_settings(tmp_path).enabled is False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
def test_disabled_service_reports_and_refuses_writes(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    service = LocalMemoryService(tmp_path)
    assert service.enabled is False
    assert service.status()["count"] == 0
    assert service.search("anything") == []
    with pytest.raises(Exception):
        service.add("should not be stored")


def test_enabled_service_stores_searches_and_deletes(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _enable(tmp_path)
    service = LocalMemoryService(tmp_path)
    assert service.enabled is True

    record = service.add("Deploys use the staging key first")
    service.add("Frontend builds live in desktop/web/dist")

    found = service.search("staging")
    assert [item.id for item in found] == [record.id]
    assert service.count() == 2

    assert service.delete(record.id) is True
    assert service.search("staging") == []
    assert service.count() == 1


def test_records_are_scoped_to_the_project(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    enabled_root = tmp_path / "one"
    other_root = tmp_path / "two"
    enabled_root.mkdir()
    other_root.mkdir()
    _enable(enabled_root)
    _enable(other_root)

    LocalMemoryService(enabled_root).add("only in project one")
    assert LocalMemoryService(other_root).search("only in project one") == []
    assert len(LocalMemoryService(enabled_root).search("only in project one")) == 1


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _client(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    return TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path)))


def test_memory_api_reports_settings_and_enables(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        status = client.get("/api/memory").json()
        assert status["settings"]["enabled"] is False
        assert status["providers"] == ["off", "sqlite"]
        assert status["count"] == 0

        enabled = client.put("/api/memory", json={"enabled": True, "provider": "sqlite", "limit": 5})
        assert enabled.status_code == 200
        assert enabled.json()["settings"] == {
            "enabled": True, "provider": "sqlite", "limit": 5,
            "auto_capture": False, "max_per_turn": 1,
        }

        captured = client.put("/api/memory", json={
            "enabled": True, "provider": "sqlite", "limit": 5,
            "auto_capture": True, "max_per_turn": 2,
        })
        assert captured.status_code == 200
        assert captured.json()["settings"]["auto_capture"] is True
        assert captured.json()["settings"]["max_per_turn"] == 2

        assert client.put(
            "/api/memory", json={"enabled": True, "provider": "mem0"},
        ).status_code == 400
        assert client.put(
            "/api/memory", json={"enabled": True, "provider": "off"},
        ).status_code == 400
        assert client.put("/api/memory", json={
            "enabled": False, "provider": "off", "auto_capture": True,
        }).status_code == 400


def test_memory_records_api_roundtrip(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.put("/api/memory", json={"enabled": True, "provider": "sqlite", "limit": 8})

        created = client.post("/api/memory/records", json={"content": "Use pnpm in this repo"})
        assert created.status_code == 200
        record_id = created.json()["record"]["id"]

        listed = client.get("/api/memory/records").json()["records"]
        assert [item["content"] for item in listed] == ["Use pnpm in this repo"]
        assert client.get("/api/memory/records?query=pnpm").json()["records"][0]["id"] == record_id

        assert client.delete(f"/api/memory/records/{record_id}").json() == {"deleted": True}
        assert client.get("/api/memory/records").json()["records"] == []


def test_memory_api_rejects_writes_while_disabled(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/memory/records", json={"content": "nope"})
        assert response.status_code == 400
        assert "disabled" in response.json()["detail"].lower()
        assert client.post("/api/memory/records", json={"content": "  "}).status_code == 400
