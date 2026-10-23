"""UI-managed model profiles: persistence, masking and HTTP surface."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core.model_profiles import (
    delete_profile,
    load_model_profiles,
    load_saved_profiles,
    profiles_path,
    upsert_profile,
)
from core.secrets import get_secret
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path)))


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def test_profiles_roundtrip_and_mask(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)

    upsert_profile("kimi", "kimi-k2", provider="openai", api_key="sk-abcdef123456", base_url="https://x/v1")
    saved = load_saved_profiles()
    assert saved["kimi"]["model"] == "kimi-k2"
    # Secrets never enter models.json; they live in the keychain/file store.
    assert saved["kimi"]["api_key"] is None
    assert get_secret("model.kimi") == "sk-abcdef123456"
    assert profiles_path().is_file()

    profiles = load_model_profiles()
    assert profiles["kimi"].api_key == "sk-abcdef123456"
    masked = profiles["kimi"].as_dict()
    assert masked["api_key"] == "***3456"
    assert masked["has_key"] is True
    assert masked["source"] == "file"


def test_empty_api_key_keeps_the_stored_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)

    upsert_profile("kimi", "kimi-k2", api_key="sk-secret-123456")
    upsert_profile("kimi", "kimi-k2-turbo", api_key="")
    assert get_secret("model.kimi") == "sk-secret-123456"
    assert load_saved_profiles()["kimi"]["model"] == "kimi-k2-turbo"


def test_invalid_profile_names_and_models(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    with pytest.raises(ValueError):
        upsert_profile("Bad Name!", "m")
    with pytest.raises(ValueError):
        upsert_profile("ok", "  ")


def test_file_profile_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("MODEL_PROFILES", "envonly,shared")
    monkeypatch.setenv("MODEL_ENVONLY_ID", "env-model")
    monkeypatch.setenv("MODEL_SHARED_ID", "env-shared")
    monkeypatch.setenv("MODEL_SHARED_API_KEY", "sk-env")

    upsert_profile("shared", "file-shared", api_key="sk-file-123456")

    profiles = load_model_profiles()
    assert profiles["envonly"].model == "env-model"
    assert profiles["shared"].model == "file-shared"
    assert profiles["shared"].api_key == "sk-file-123456"


def test_delete_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    upsert_profile("gone", "m")
    assert delete_profile("gone") is True
    assert delete_profile("gone") is False
    assert load_saved_profiles() == {}


def test_corrupt_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    path = profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops", encoding="utf-8")
    assert load_saved_profiles() == {}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_http_roundtrip_never_returns_raw_keys(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        created = client.put("/api/settings/models", json={
            "name": "deepseek", "model": "deepseek-v4", "provider": "deepseek",
            "api_key": "sk-super-secret-9999", "base_url": "https://api.deepseek.com",
        })
        assert created.status_code == 200
        assert created.json()["profile"]["api_key"] == "***9999"
        assert "sk-super-secret" not in created.text

        listed = client.get("/api/settings/models").json()
        assert [item["name"] for item in listed["profiles"]] == ["deepseek"]
        assert listed["file_profiles"] == ["deepseek"]
        assert "sk-super-secret" not in json.dumps(listed)

        assert client.get("/api/models").json()[0]["name"] == "deepseek"
        assert client.delete("/api/settings/models/deepseek").json() == {"deleted": True}
        assert client.get("/api/settings/models").json()["profiles"] == []


def test_http_rejects_invalid_profiles(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.put("/api/settings/models", json={
            "name": "Bad Name", "model": "x",
        }).status_code == 400
        assert client.put("/api/settings/models", json={
            "name": "ok", "model": "",
        }).status_code == 400
