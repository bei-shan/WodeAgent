"""Plugin lifecycle, safe Skills loading, trust gating and API regressions."""
from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.plugin_loader import PluginCatalog, PluginLoader
from core.skills.skill_loader import SkillLoader
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def bundle(name="review-kit", *, manifest=None, extras=None):
    manifest = manifest or {
        "name": name, "version": "1.1", "description": "Code review guidance",
        "features": {"skills": ["skills"]},
    }
    files = {"plugin.json": json.dumps(manifest),
             "skills/review/SKILL.md": "---\nname: review\ndescription: Review code\n---\n# Review\n",
             "skills/review/references/checklist.md": "DO NOT DROP RESOURCE"}
    files.update(extras or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for filename, data in files.items():
            z.writestr(filename, data)
    return buf.getvalue()


def test_install_disabled_by_default_then_scan_skill_enable_disable(tmp_path):
    catalog = PluginCatalog(tmp_path)
    result = catalog.install_zip(bundle())
    assert result == {"status": "installed", "name": "review-kit", "enabled": False}
    assert catalog.list_plugins()[0]["enabled"] is False
    assert PluginLoader(str(tmp_path)).discover() == []
    loader = SkillLoader(str(tmp_path))
    assert loader.scan() == []
    catalog.set_enabled("review-kit", True)
    assert [s.name for s in loader.refresh_if_stale()] == ["review"]
    assert (tmp_path / ".mycode/plugins/review-kit/skills/review/references/checklist.md").read_text() == "DO NOT DROP RESOURCE"
    assert "review" in loader.format_skills_for_prompt(1000)
    catalog.set_enabled("review-kit", False)
    assert loader.refresh_if_stale() == []
    assert not list((tmp_path / ".mycodeagent/skills").glob("review/**"))
    catalog.set_enabled("review-kit", True)
    assert [s.name for s in loader.refresh_if_stale()] == ["review"]
    catalog.uninstall("review-kit")
    assert catalog.list_plugins() == []
    assert loader.refresh_if_stale() == []


def test_disabled_untrusted_python_never_imported_even_after_enable(tmp_path, monkeypatch):
    marker = tmp_path / "EXECUTED"
    manifest = {
        "name": "code-kit",
        "features": {"skills": ["skills"], "custom_features": ["danger.py"]},
    }
    script = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(bundle("code-kit", manifest=manifest, extras={"danger.py": script}))
    assert PluginLoader(str(tmp_path)).discover() == []
    catalog.set_enabled("code-kit", True)
    assert PluginLoader(str(tmp_path)).discover() == []
    assert not marker.exists()
    catalog.set_trusted("code-kit", True)
    assert PluginLoader(str(tmp_path)).discover() == []  # env not set
    assert not marker.exists()
    catalog.set_trusted("code-kit", False)
    monkeypatch.setenv("RIDGE_ALLOW_PLUGIN_CODE", "1")
    assert PluginLoader(str(tmp_path)).discover() == []  # trust revoked
    assert not marker.exists()


def test_trusted_python_feature_requires_two_explicit_gates(tmp_path, monkeypatch):
    manifest = {"name": "my-feature", "features": {"custom_features": ["feature.py"]}}
    src = ("from core.features.base import AgentFeature\n"
           "class ThirdParty(AgentFeature):\n"
           "    name = 'thirdparty'\n"
           "    order = 50\n")
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(bundle("my-feature", manifest=manifest, extras={"feature.py": src}))
    catalog.set_enabled("my-feature", True)
    catalog.set_trusted("my-feature", True)
    assert PluginLoader(str(tmp_path)).discover() == []
    monkeypatch.setenv("RIDGE_ALLOW_PLUGIN_CODE", "1")
    features = PluginLoader(str(tmp_path)).discover()
    assert [x.name for x in features] == ["thirdparty"]


def test_trusted_plugin_hooks_register_in_memory_no_user_config_changes(tmp_path, monkeypatch):
    manifest = {
        "name": "hooks-addon",
        "features": {"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "echo ok"}
            ]}],
        }},
    }
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(bundle("hooks-addon", manifest=manifest))
    catalog.set_enabled("hooks-addon", True)
    catalog.set_trusted("hooks-addon", True)
    monkeypatch.setenv("RIDGE_ALLOW_PLUGIN_CODE", "1")
    features = PluginLoader(str(tmp_path)).discover()
    assert len(features) == 1
    from core.hook_system import HookManager
    manager = HookManager(str(tmp_path))
    config_path = tmp_path / ".mycode/hooks.json"
    agent = type("Host", (), {"_hook_manager": manager})()
    features[0].init(agent)
    assert len(manager._config.pre_tool_use) == 1
    assert not config_path.exists()


@pytest.mark.parametrize("filename", [
    "../escape.txt", "/absolute.txt", "skills/../../escape",
    ".git/config", "skills\\evil", "scripts/.secret",
])
def test_zip_path_validation_rejects_traversal_and_hidden_files(tmp_path, filename):
    with pytest.raises(ValueError, match="Unsafe"):
        PluginCatalog(tmp_path).install_zip(bundle(extras={filename: "malicious"}))
    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlinks_and_oversize_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("plugin.json", '{"name":"evil-kit"}')
        info = zipfile.ZipInfo("skills/evil/SKILL.md")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        out.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="link"):
        PluginCatalog(tmp_path).install_zip(buf.getvalue())
    with pytest.raises(ValueError, match="2 MiB"):
        PluginCatalog(tmp_path).install_zip(b"X" * (2 * 1024 * 1024 + 1))


def test_invalid_component_relative_paths_rejected_and_install_rolled_back(tmp_path):
    catalog = PluginCatalog(tmp_path)
    with pytest.raises(ValueError):
        catalog.install_zip(bundle(manifest={
            "name": "review-kit", "features": {"skills": ["../../escape"]}
        }))
    assert catalog.list_plugins() == []
    assert not (tmp_path / ".mycode/plugins/review-kit").exists()


def test_duplicate_install_and_name_mismatch_rejected(tmp_path):
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(bundle())
    with pytest.raises(FileExistsError):
        catalog.install_zip(bundle())
    with pytest.raises(ValueError):
        catalog.install_zip(bundle(manifest={"name": "Invalid_Name"}))


def test_symlinked_plugin_directory_never_discovered(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-plugin-external")
    outside.mkdir()
    (outside / "plugin.json").write_text('{"name":"external"}')
    plugin_root = tmp_path / ".mycode/plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "external").symlink_to(outside, target_is_directory=True)
    assert PluginCatalog(tmp_path).list_plugins() == []


def test_builtin_seed_copies_references_without_following_symlinks(tmp_path):
    origin = tmp_path / "skills/sample"
    (origin / "references").mkdir(parents=True)
    (origin / "SKILL.md").write_text("---\nname: sample\ndescription: Sample skill\n---\n")
    (origin / "references/checks.txt").write_text("important")
    external = tmp_path.parent / (tmp_path.name + "-secret.txt")
    external.write_text("secret")
    (origin / "references/leak.txt").symlink_to(external)
    loader = SkillLoader(str(tmp_path))
    assert [s.name for s in loader.scan()] == ["sample"]
    runtime = tmp_path / ".mycodeagent/skills/sample"
    assert (runtime / "references/checks.txt").read_text() == "important"
    assert not (runtime / "references/leak.txt").exists()


def test_plugin_http_install_activate_uninstall_and_data_only_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("RIDGE_ALLOW_PLUGIN_CODE", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/plugins").json() == []
        upload = client.post("/api/plugins/install", files={
            "file": ("review-kit.zip", bundle(), "application/zip")
        })
        assert upload.status_code == 201, upload.text
        assert upload.json()["enabled"] is False
        assert client.post("/api/plugins/install", files={
            "file": ("duplicate.zip", bundle(), "application/zip")
        }).status_code == 409
        row = client.get("/api/plugins").json()[0]
        assert row["name"] == "review-kit"
        assert row["enabled"] is False
        assert client.put("/api/plugins/review-kit/enabled", json={"enabled": True}).json() == {
            "name": "review-kit", "enabled": True
        }
        assert "review" in {x["name"] for x in client.get("/api/skills").json()}
        assert client.get("/api/skills/review/content").status_code == 200
        assert client.delete("/api/skills/review").status_code == 403
        assert client.put("/api/plugins/review-kit/enabled", json={"enabled": False}).status_code == 200
        assert "review" not in {x["name"] for x in client.get("/api/skills").json()}
        assert client.put("/api/plugins/review-kit/enabled", json={"enabled": "yes"}).status_code == 400
        assert client.delete("/api/plugins/review-kit").status_code == 200
        assert client.get("/api/plugins").json() == []


def test_api_rejects_archive_traversal_and_unknown_plugins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        resp = client.post("/api/plugins/install", files={
            "file": ("bad.zip", bundle(extras={"../private": "oops"}), "application/zip")
        })
        assert resp.status_code == 400
        assert client.put("/api/plugins/missing/enabled", json={"enabled": True}).status_code == 404
        assert client.delete("/api/plugins/missing").status_code == 404
