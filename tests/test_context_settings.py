"""Compaction settings: validation, persistence and Config overlay."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.config import Config
from core.context_settings import (
    ContextSettingsError,
    load_settings,
    save_settings,
)
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))


def test_defaults_and_roundtrip():
    assert load_settings().compression_threshold == 0.8
    saved = save_settings({
        "compression_threshold": 0.9,
        "min_retain_rounds": 6,
        "tool_prune_keep_rounds": 2,
    })
    assert saved.compression_threshold == 0.9
    assert saved.min_retain_rounds == 6
    assert load_settings() == saved


def test_invalid_values_are_rejected():
    with pytest.raises(ContextSettingsError):
        save_settings({"compression_threshold": 1.5})
    with pytest.raises(ContextSettingsError):
        save_settings({"min_retain_rounds": 1})
    with pytest.raises(ContextSettingsError):
        save_settings({"tool_prune_soft_ratio": 0.95})  # must be below summary threshold
    with pytest.raises(ContextSettingsError):
        save_settings({"tool_prune_keep_rounds": 99})


def test_config_from_env_applies_the_file_overrides():
    save_settings({"compression_threshold": 0.7, "min_retain_rounds": 4})
    config = Config.from_env()
    assert config.compression_threshold == 0.7
    assert config.min_retain_rounds == 4


def test_context_settings_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    with TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path))) as client:
        assert client.get("/api/settings/context").json()["settings"]["compression_threshold"] == 0.8
        updated = client.put("/api/settings/context", json={
            "compression_threshold": 0.85, "min_retain_rounds": 8,
        })
        assert updated.status_code == 200
        assert updated.json()["settings"]["compression_threshold"] == 0.85
        assert client.put("/api/settings/context", json={
            "compression_threshold": 0.99,
        }).status_code == 400
