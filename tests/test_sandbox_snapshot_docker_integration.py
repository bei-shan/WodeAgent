"""Real Docker snapshot acceptance: guest writes never mutate the live host workspace."""
from __future__ import annotations

import os
from pathlib import Path
import pytest

from core.sandbox.docker_provider import DockerCommandProvider
from core.sandbox.contracts import SandboxUnavailable


def test_real_docker_snapshot_is_private_and_export_requires_approval(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.txt").write_text("trusted-input", encoding="utf-8")
    provider = DockerCommandProvider(root, snapshot_workspace=True)
    stage = provider.workspace
    try:
        result = provider.execute("cat source.txt; echo guest-result > generated.txt")
        assert result.exit_code == 0, result.stderr
        assert "trusted-input" in result.stdout
        assert not (root / "generated.txt").exists(), "guest wrote into live host workspace"
        assert (stage / "generated.txt").read_text().strip() == "guest-result"
        with pytest.raises(PermissionError):
            provider.export_guest_file("generated.txt", authorize=lambda *_: False)
        assert provider.export_guest_file("generated.txt", authorize=lambda *_: True).strip() == b"guest-result"
        assert not (root / "generated.txt").exists()
        (root / "source.txt").write_text("host-race", encoding="utf-8")
        result = provider.execute("cat source.txt")
        assert result.exit_code == 0 and "trusted-input" in result.stdout
    finally:
        provider.close()
    assert not stage.exists()
