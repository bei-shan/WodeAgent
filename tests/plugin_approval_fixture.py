"""Isolated test-only operator provisioner for plugin executable consent."""
from __future__ import annotations

from pathlib import Path


def provision_plugin_approval_key(monkeypatch, root: Path) -> Path:
    home = root.parent / ("operator-home-" + root.name)
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    config = home / ".config"
    config.mkdir(mode=0o700, exist_ok=True)
    config.chmod(0o700)
    folder = config / "ridgecode"
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    key = folder / "plugin-approval.key"
    key.write_bytes(bytes(range(32)))
    key.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return key
