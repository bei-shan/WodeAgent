"""Disposable Docker guest workspace cannot mutate the host by default."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.docker_provider import DockerCommandProvider
from core.sandbox.workspace_snapshot import SandboxWorkspaceSnapshot


def test_snapshot_changes_and_explicit_conflict_checked_export(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "hello.py").write_text("print('old')")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("fake git config")
    with SandboxWorkspaceSnapshot(root) as snapshot:
        assert not (snapshot.workspace / ".git").exists()
        guest = snapshot.workspace / "src" / "hello.py"
        guest.write_text("print('new')")
        assert (root / "src" / "hello.py").read_text() == "print('old')"
        assert snapshot.changed() == ("src/hello.py",)
        with pytest.raises(PermissionError, match="approval"):
            snapshot.export("src/hello.py", authorize=lambda *_: False)
        assert snapshot.export("src/hello.py", authorize=lambda *_: True) == b"print('new')"
        (root / "src" / "hello.py").write_text("concurrent host write")
        with pytest.raises(PermissionError, match="changed"):
            snapshot.export("src/hello.py", authorize=lambda *_: True)
    assert not snapshot.workspace.exists()


def test_snapshot_refuses_symlinks_special_files_and_secret_names(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "safe.txt").write_text("safe")
    (root / "alias.txt").symlink_to(tmp_path / "private")
    with pytest.raises(SandboxUnavailable, match="symlinks"):
        SandboxWorkspaceSnapshot(root)
    (root / "alias.txt").unlink()
    (root / ".env.production").write_text("secret")
    with pytest.raises(SandboxUnavailable):
        SandboxWorkspaceSnapshot(root)


def test_snapshot_rejects_bounded_file_size_and_guest_symlink(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "big.bin").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(SandboxUnavailable, match="oversized"):
        SandboxWorkspaceSnapshot(root)
    (root / "big.bin").unlink()
    with SandboxWorkspaceSnapshot(root) as snapshot:
        (snapshot.workspace / "evil").symlink_to(tmp_path / "outside")
        with pytest.raises(SandboxUnavailable, match="symlink"):
            snapshot.changed()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX Docker snapshot files")
def test_docker_command_argv_uses_private_copy_not_host(tmp_path, monkeypatch):
    if os.getuid() == 0:
        pytest.skip("root hosts cannot safely mount a private snapshot as a non-root guest")
    root = tmp_path / "project"
    root.mkdir()
    (root / "input.txt").write_text("source")
    output = []
    def fake(argv, **_):
        output.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    provider = DockerCommandProvider(
        root, docker_binary="/usr/bin/docker", runner=fake,
        snapshot_workspace=True,
    )
    try:
        assert provider.workspace != root
        assert (provider.workspace / "input.txt").read_text() == "source"
        result = provider.execute("echo guest > guest.txt")
        assert result.exit_code == 0
        argv = output[0]
        mount = argv[argv.index("--mount") + 1]
        assert f"source={provider.workspace},target=/workspace" in mount
        assert f"source={root},target=/workspace" not in mount
        (provider.workspace / "result.txt").write_bytes(b"guest-only")
        assert not (root / "result.txt").exists()
        assert provider.export_guest_file("result.txt", authorize=lambda *_: True) == b"guest-only"
    finally:
        stage = provider.workspace
        provider.close()
        assert not stage.exists()
