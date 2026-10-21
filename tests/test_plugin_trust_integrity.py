"""Consent to executable plugins must bind to reviewed file bytes."""
from __future__ import annotations

import json

import pytest

from core.plugin_loader import PluginCatalog, PluginLoader
from tests.test_plugin_lifecycle import bundle
from tests.plugin_approval_fixture import provision_plugin_approval_key


def code_bundle(name="fingerprint-test"):
    return bundle(name, manifest={
        "name": name,
        "version": "1.0.0",
        "components": {
            "skills": ["skills"],
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "echo reviewed"}
                ]}
            ]},
        },
    }, extras={"entry.py": (
        "from pathlib import Path\n"
        "Path('/tmp/project-python-should-never-import').write_text('unsafe')\n"
    )})


def test_trust_is_bound_to_reviewed_code_and_can_be_reauthorized(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDGE_ALLOW_TRUSTED_HOOKS", "1")
    provision_plugin_approval_key(monkeypatch, tmp_path)
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(code_bundle())
    catalog.set_enabled("fingerprint-test", True)
    assert not catalog.list_plugins()[0]["trust_valid"]
    assert PluginLoader(str(tmp_path)).discover() == []
    catalog.set_trusted("fingerprint-test", True)
    assert catalog.list_plugins()[0]["trust_valid"]
    assert catalog.hooks_trusted("fingerprint-test")
    assert [item.name for item in PluginLoader(str(tmp_path)).discover()] == ["plugin_hooks"]

    entry = tmp_path / ".mycode/plugins/fingerprint-test/entry.py"
    entry.write_text(entry.read_text() + "\n# changed after review\n")
    listed = catalog.list_plugins()[0]
    assert listed["trusted"] is True  # recorded consent still visible
    assert listed["trust_valid"] is False
    assert not catalog.hooks_trusted("fingerprint-test")
    assert PluginLoader(str(tmp_path)).discover() == []

    # Re-review and re-consent to the updated content.
    catalog.set_trusted("fingerprint-test", False)
    catalog.set_trusted("fingerprint-test", True)
    assert catalog.hooks_trusted("fingerprint-test")
    assert [item.name for item in PluginLoader(str(tmp_path)).discover()] == ["plugin_hooks"]


def test_changed_manifest_or_supporting_asset_invalidates_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDGE_ALLOW_TRUSTED_HOOKS", "1")
    provision_plugin_approval_key(monkeypatch, tmp_path)
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(code_bundle("asset-review"))
    catalog.set_enabled("asset-review", True)
    catalog.set_trusted("asset-review", True)
    folder = tmp_path / ".mycode/plugins/asset-review"
    assert catalog.hooks_trusted("asset-review")

    ref = folder / "skills/review/references/checklist.md"
    ref.write_text(ref.read_text() + "\nnew instructions\n")
    assert not catalog.hooks_trusted("asset-review")
    ref.write_text("DO NOT DROP RESOURCE")
    assert catalog.hooks_trusted("asset-review")

    manifest = folder / "plugin.json"
    data = json.loads(manifest.read_text())
    data["description"] = "modified manifest"
    manifest.write_text(json.dumps(data))
    assert not catalog.hooks_trusted("asset-review")


def test_unverified_trust_flag_is_not_sufficient(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDGE_ALLOW_TRUSTED_HOOKS", "1")
    provision_plugin_approval_key(monkeypatch, tmp_path)
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(code_bundle("legacy-review"))
    catalog.set_enabled("legacy-review", True)
    state = catalog._state()
    state["legacy-review"]["trusted"] = True
    state["legacy-review"].pop("trusted_digest", None)
    catalog._write_state(state)
    assert catalog.list_plugins()[0]["trusted"] is True
    assert catalog.list_plugins()[0]["trust_valid"] is False
    assert not catalog.hooks_trusted("legacy-review")
    assert PluginLoader(str(tmp_path)).discover() == []


def test_symlinked_file_after_consent_invalidates_executable_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDGE_ALLOW_TRUSTED_HOOKS", "1")
    provision_plugin_approval_key(monkeypatch, tmp_path)
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(code_bundle("link-review"))
    catalog.set_enabled("link-review", True)
    catalog.set_trusted("link-review", True)
    outside = tmp_path.parent / (tmp_path.name + "-outside-plugin-source.py")
    outside.write_text("print('not executed')\n")
    ref = tmp_path / ".mycode/plugins/link-review/sneaky.py"
    try:
        ref.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable")
    assert not catalog.hooks_trusted("link-review")
    assert PluginLoader(str(tmp_path)).discover() == []
    assert outside.read_text() == "print('not executed')\n"


def test_content_consent_requires_small_bounded_regular_files(tmp_path):
    catalog = PluginCatalog(tmp_path)
    catalog.install_zip(code_bundle("large-review"))
    payload = tmp_path / ".mycode/plugins/large-review/oversized.txt"
    payload.write_bytes(b"z" * (6 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds size"):
        catalog.set_trusted("large-review", True)
    assert not catalog.list_plugins()[0]["trust_valid"]
