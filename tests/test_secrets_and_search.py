"""Secrets (keychain/file), search provider selection, and their HTTP surface."""
from __future__ import annotations

import builtins
import json

import pytest
from fastapi.testclient import TestClient

from core import secrets
from core.secrets import delete_secret, get_secret, mask, set_secret
from core.web_search_config import (
    load_search_settings,
    save_search_settings,
    provider_status,
    SearchSettingsError,
)
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


class FakeKeyring:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.values:
            raise KeyError(name)
        del self.values[(service, name)]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in (
        "TAVILY_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY", "EXA_API_KEY",
        "SEARXNG_BASE_URL", "WEB_SEARCH_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def no_keyring(monkeypatch):
    """Force the 0600 file fallback by making the keyring import fail."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("no keyring in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.fixture()
def fake_keyring(monkeypatch):
    backend = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: backend)
    return backend


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    return TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path)))


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def test_file_fallback_roundtrip_and_permissions(tmp_path, monkeypatch, no_keyring):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    assert set_secret("TAVILY_API_KEY", "tvly-123456789") == "file"
    assert get_secret("TAVILY_API_KEY") == "tvly-123456789"
    assert secrets.secrets_path().is_file()
    assert delete_secret("TAVILY_API_KEY") is True
    assert get_secret("TAVILY_API_KEY") is None


def test_keyring_preferred_and_file_copy_removed(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    assert set_secret("TAVILY_API_KEY", "tvly-secret") == "keyring"
    assert fake_keyring.values[(secrets.SERVICE, "TAVILY_API_KEY")] == "tvly-secret"
    assert get_secret("TAVILY_API_KEY") == "tvly-secret"
    assert delete_secret("TAVILY_API_KEY") is True
    assert get_secret("TAVILY_API_KEY") is None


def test_environment_is_the_last_fallback(tmp_path, monkeypatch, no_keyring):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")
    assert get_secret("TAVILY_API_KEY") == "tvly-from-env"


def test_mask_never_reveals_short_secrets():
    assert mask("short") == "***"
    assert mask("sk-123456789012") == "***9012"
    assert mask(None) is None


# ---------------------------------------------------------------------------
# Search settings
# ---------------------------------------------------------------------------
def test_search_settings_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)

    assert load_search_settings()["provider"] == "tavily"
    assert save_search_settings("duckduckgo")["provider"] == "duckduckgo"
    assert load_search_settings()["provider"] == "duckduckgo"

    with pytest.raises(SearchSettingsError):
        save_search_settings("nope")
    with pytest.raises(SearchSettingsError):
        save_search_settings("searxng", "")

    settings = save_search_settings("searxng", "http://localhost:8080")
    assert settings["searxng_base_url"] == "http://localhost:8080"


def test_provider_status_marks_configured_providers(tmp_path, monkeypatch, no_keyring):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYCODEAGENT_DATA_DIR", raising=False)
    set_secret("BRAVE_API_KEY", "brave-secret-123456")
    status = {item["name"]: item for item in provider_status()}
    assert status["brave"]["configured"] is True
    assert status["tavily"]["configured"] is False
    assert status["duckduckgo"]["requires_key"] is False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_search_and_keys_api(tmp_path, monkeypatch, no_keyring):
    with _client(tmp_path, monkeypatch) as client:
        updated = client.put("/api/settings/search", json={"provider": "brave"})
        assert updated.status_code == 200
        assert updated.json()["provider"] == "brave"
        assert {item["name"] for item in updated.json()["providers"]} >= {"tavily", "brave"}

        saved = client.put("/api/settings/keys", json={
            "name": "BRAVE_API_KEY", "value": "brave-123456789",
        })
        assert saved.status_code == 200
        assert saved.json()["backend"] == "file"

        listed = client.get("/api/settings/keys").json()["keys"]
        brave = next(item for item in listed if item["name"] == "BRAVE_API_KEY")
        assert brave["configured"] is True
        assert brave["api_key"] == "***6789"
        assert "brave-123456789" not in json.dumps(listed)

        assert client.put("/api/settings/keys", json={
            "name": "NOT_ALLOWED", "value": "x",
        }).status_code == 400
        assert client.delete("/api/settings/keys/BRAVE_API_KEY").json()["deleted"] is True


def test_model_test_endpoint_reports_success_and_failure(tmp_path, monkeypatch):
    from core import llm as llm_module

    class FakeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def invoke(self, messages):
            if self.kwargs.get("model") == "broken":
                raise RuntimeError("connection refused")
            return "OK"

    monkeypatch.setattr(llm_module, "HelloAgentsLLM", FakeLLM)

    with _client(tmp_path, monkeypatch) as client:
        client.put("/api/settings/models", json={
            "name": "good", "model": "good-model", "api_key": "sk-test-123456",
        })
        client.put("/api/settings/models", json={
            "name": "bad", "model": "broken", "api_key": "sk-test-654321",
        })

        good = client.post("/api/settings/models/good/test").json()
        assert good["ok"] is True
        assert good["message"] == "OK"

        bad = client.post("/api/settings/models/bad/test").json()
        assert bad["ok"] is False
        assert "connection refused" in bad["message"]

        assert client.post("/api/settings/models/missing/test").status_code == 400
