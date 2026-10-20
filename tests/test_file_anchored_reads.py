"""Secure FD anchored reads cannot follow swapped external symlink targets."""
from __future__ import annotations

import os

import pytest

from core.runtime.file_commit import (
    ConcurrentFileChange, open_regular_workspace_file, read_workspace_bytes,
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd symlink hardening")
def test_anchored_read_does_not_follow_symlinked_file_or_parent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    directory = root / "docs"
    directory.mkdir()
    (directory / "public.txt").write_text("public")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "public.txt").write_text("SECRET")

    assert read_workspace_bytes(directory / "public.txt", root) == b"public"
    (directory / "public.txt").unlink()
    (directory / "public.txt").symlink_to(outside / "public.txt")
    with pytest.raises(OSError):
        read_workspace_bytes(directory / "public.txt", root)

    (directory / "public.txt").unlink()
    directory.rmdir()
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_workspace_bytes(directory / "public.txt", root)


def test_edit_source_size_is_bounded_without_reading_huge_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    file = root / "huge.txt"
    with file.open("wb") as stream:
        stream.truncate(1025)
    with pytest.raises(ValueError, match="edit limit"):
        read_workspace_bytes(file, root, limit_bytes=1024)


def test_anchored_text_stream_preserves_utf8_and_newlines(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "x.txt").write_bytes("你好\r\nbye\n".encode())
    with open_regular_workspace_file(root / "x.txt", root, text=True) as src:
        assert src.read() == "你好\nbye\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor anchoring")
def test_posix_fd_features_are_detected_even_when_replace_not_listed():
    # os.replace has src_dir_fd/dst_dir_fd but CPython 3.12's capability
    # registry lists os.rename instead of os.replace.
    from core.runtime.file_commit import _ANCHOR_SUPPORTED
    assert _ANCHOR_SUPPORTED is True
