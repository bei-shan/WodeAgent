"""Anchored workspace commits reject symlink escape and concurrent publishes."""
from __future__ import annotations

import os
import stat

import pytest

from core.runtime.file_commit import (
    _ANCHOR_SUPPORTED, ConcurrentFileChange, commit_utf8,
)


@pytest.mark.skipif(not _ANCHOR_SUPPORTED, reason="POSIX dirfd required")
def test_anchored_commit_preserves_mode_and_rejects_symlink_parent(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    folder = root / "sub"
    folder.mkdir()
    target = folder / "hello.txt"
    target.write_text("old")
    target.chmod(0o750)
    before = target.stat()
    n = commit_utf8(target, "new", expected_stat=before, workspace_root=root)
    assert n == 3
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    # The lexical parent was replaced after an earlier resolve(). Anchored
    # traversal must not follow it into the untrusted outside directory.
    folder.rename(root / "moved")
    folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        commit_utf8(target, "hacked", expected_stat=None, workspace_root=root)
    assert not (outside / "hello.txt").exists()


@pytest.mark.skipif(not _ANCHOR_SUPPORTED, reason="POSIX dirfd required")
def test_anchored_exclusive_create_not_overwrite_existing(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "new.txt"
    assert commit_utf8(target, "first", expected_stat=None, workspace_root=root) == 5
    with pytest.raises((ConcurrentFileChange, FileExistsError)):
        commit_utf8(target, "second", expected_stat=None, workspace_root=root)
    assert target.read_text() == "first"
    assert not list(root.glob(".*.ridge-write"))
