"""Plugin provenance, immutable pinning and failed-upgrade rollback contracts."""
from __future__ import annotations

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from core.plugin_loader import PluginCatalog
from tests.test_plugin_lifecycle import bundle
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def version_zip(version: str, *, name: str = "review-kit") -> bytes:
    return bundle(name=name, manifest={
        "name": name, "version": version, "components": {"skills": ["skills"]},
    }, extras={"skills/review/references/version.md": version})


def test_manifest_source_checksum_and_version_pin(tmp_path):
    catalog = PluginCatalog(tmp_path)
    old = version_zip("1.0.0")
    catalog.install_zip(old)
    current = catalog.list_plugins()[0]
    assert current["version"] == "1.0.0"
    assert current["source"] == "local_zip"
    assert current["sha256"] == hashlib.sha256(old).hexdigest()
    assert current["pinned"] is False
    assert current["rollback_available"] is False
    catalog.set_pinned("review-kit", True)
    with pytest.raises(PermissionError, match="Unpin"):
        catalog.update_zip("review-kit", version_zip("1.1.0"))
    assert catalog.list_plugins()[0]["version"] == "1.0.0"
    catalog.set_pinned("review-kit", False)


def test_update_revokes_trust_disables_new_code_and_rollback(tmp_path):
    catalog = PluginCatalog(tmp_path)
    a = version_zip("1.0.0")
    b = version_zip("1.1.0")
    catalog.install_zip(a)
    catalog.set_enabled("review-kit", True)
    catalog.set_trusted("review-kit", True)
    result = catalog.update_zip("review-kit", b, expected_sha256=hashlib.sha256(b).hexdigest())
    assert result == {
        "status": "updated", "name": "review-kit", "version": "1.1.0",
        "enabled": False, "trusted": False, "sha256": hashlib.sha256(b).hexdigest(),
    }
    plugin = catalog.list_plugins()[0]
    assert plugin["rollback_available"] is True
    assert plugin["previous_version"] == "1.0.0"
    assert not plugin["enabled"] and not plugin["trusted"]
    assert (tmp_path / ".mycode/plugins/review-kit/skills/review/references/version.md").read_text() == "1.1.0"

    rolled = catalog.rollback("review-kit")
    assert rolled["version"] == "1.0.0"
    assert catalog.list_plugins()[0]["trusted"] is False
    assert catalog.list_plugins()[0]["enabled"] is False
    assert (tmp_path / ".mycode/plugins/review-kit/skills/review/references/version.md").read_text() == "1.0.0"
    catalog.uninstall("review-kit")
    assert not (tmp_path / ".mycodeagent/plugins/backups/review-kit").exists()


def test_reject_same_version_wrong_name_or_sha_without_mutation(tmp_path):
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(version_zip("1.0.0"))
    with pytest.raises(ValueError, match="checksum"):
        catalog.update_zip("review-kit", version_zip("1.1.0"), expected_sha256="a" * 64)
    with pytest.raises(ValueError, match="version"):
        catalog.update_zip("review-kit", version_zip("1.0.0"))
    with pytest.raises(ValueError, match="package name"):
        catalog.update_zip("review-kit", version_zip("1.1.0", name="other-kit"))
    assert catalog.list_plugins()[0]["version"] == "1.0.0"
    assert not catalog._backup_path("review-kit").exists()


def test_failed_directory_publish_restores_existing_plugin(tmp_path, monkeypatch):
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(version_zip("1.0.0"))
    real_replace = os.replace
    target = str(tmp_path / ".mycode/plugins/review-kit")
    def fail_update(src, dst):
        if str(dst) == target and ".plugin-upgrade-" in str(src):
            raise OSError("simulated failed publish")
        return real_replace(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_update)
        with pytest.raises(OSError, match="failed publish"):
            catalog.update_zip("review-kit", version_zip("1.1.0"))
    assert catalog.list_plugins()[0]["version"] == "1.0.0"
    assert not catalog._backup_path("review-kit").exists()


def test_authenticated_plugin_update_and_pin_http_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        a, b = version_zip("1.0.0"), version_zip("1.1.0")
        assert client.post("/api/plugins/install", files={
            "file": ("a.zip", a, "application/zip")
        }).status_code == 201
        assert client.put("/api/plugins/review-kit/pinned", json={"pinned": True}).status_code == 200
        assert client.post("/api/plugins/review-kit/update", files={
            "file": ("b.zip", b, "application/zip")
        }).status_code == 403
        assert client.put("/api/plugins/review-kit/pinned", json={"pinned": False}).status_code == 200
        assert client.post("/api/plugins/review-kit/update?sha256=wrong", files={
            "file": ("b.zip", b, "application/zip")
        }).status_code == 400
        assert client.post("/api/plugins/review-kit/update", files={
            "file": ("b.zip", b, "application/zip")
        }).json()["version"] == "1.1.0"
        assert client.get("/api/plugins").json()[0]["rollback_available"] is True
        assert client.post("/api/plugins/review-kit/rollback").json()["version"] == "1.0.0"
        assert client.put("/api/plugins/review-kit/pinned", json={"pinned": "yes"}).status_code == 400
