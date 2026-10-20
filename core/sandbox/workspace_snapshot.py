"""Bounded disposable sandbox workspace copy, never a live full RW host mount.

The snapshot is private and ephemeral; guest writes cannot mutate the source
workspace. Export requires a separate trusted approval and conflict check.
This does NOT eliminate concurrent host filesystem races during an approved
writeback; privileged multi-tenant workers must not enable host writeback.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Callable

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.workspace_secrets import assert_no_mounted_credentials

_EXCLUDED = frozenset({".git", ".pytest_cache", "__pycache__", "node_modules", ".venv", ".idea"})
_MAX_ENTRIES = 20000
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or len(relative) > 1024:
        raise PermissionError("Invalid workspace-relative path")
    parts = PurePosixPath(relative).parts
    if not parts or any(p in {"", ".", ".."} or "\\" in p for p in parts):
        raise PermissionError("Invalid sandbox relative path")
    return parts


class SandboxWorkspaceSnapshot:
    """Private snapshot with a per-file baseline for conflict-aware export."""

    def __init__(self, source: str | Path):
        self.source = Path(source).resolve(strict=True)
        if not self.source.is_dir():
            raise SandboxUnavailable("Source workspace is not a directory")
        assert_no_mounted_credentials(self.source)
        self._temp = tempfile.TemporaryDirectory(prefix="ridge-guest-workspace-")
        self.workspace = Path(self._temp.name) / "workspace"
        self.workspace.mkdir(mode=0o700)
        self._baseline: dict[str, str] = {}
        self._closed = False
        try:
            self._populate()
        except BaseException:
            self.close()
            raise

    def _populate(self) -> None:
        entries = total = 0
        def walk(src: Path, dst: Path, prefix: str) -> None:
            nonlocal entries, total
            with os.scandir(src) as scandir:
                for child in scandir:
                    if child.name in _EXCLUDED:
                        continue
                    entries += 1
                    if entries > _MAX_ENTRIES:
                        raise SandboxUnavailable("Snapshot contains too many entries")
                    if child.is_symlink():
                        raise SandboxUnavailable("Snapshot refuses all symlinks")
                    rel = f"{prefix}/{child.name}" if prefix else child.name
                    if child.is_dir(follow_symlinks=False):
                        (dst / child.name).mkdir(mode=0o700)
                        walk(Path(child.path), dst / child.name, rel)
                        continue
                    if not child.is_file(follow_symlinks=False):
                        raise SandboxUnavailable("Snapshot refuses special files")
                    fd = os.open(child.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_FILE_BYTES:
                            raise SandboxUnavailable("Unsafe or oversized workspace file")
                        total += info.st_size
                        if total > _MAX_TOTAL_BYTES:
                            raise SandboxUnavailable("Snapshot exceeds quota")
                        with os.fdopen(fd, "rb", closefd=False) as handle:
                            content = handle.read(_MAX_FILE_BYTES + 1)
                        if len(content) != info.st_size:
                            raise SandboxUnavailable("Workspace file changed during snapshot")
                    finally:
                        os.close(fd)
                    (dst / child.name).write_bytes(content)
                    (dst / child.name).chmod(0o600 | (info.st_mode & 0o100))
                    self._baseline[rel] = _digest(content)
        walk(self.source, self.workspace, "")

    def changed(self) -> tuple[str, ...]:
        if self._closed:
            raise SandboxUnavailable("Snapshot already closed")
        changed: list[str] = []
        current: set[str] = set()
        for path in self.workspace.rglob("*"):
            if path.is_symlink():
                raise SandboxUnavailable("Snapshot guest added a symlink")
            if not path.is_file():
                if not path.is_dir():
                    raise SandboxUnavailable("Snapshot contains an unsafe guest file")
                continue
            rel = path.relative_to(self.workspace).as_posix()
            _safe_parts(rel)
            current.add(rel)
            data = path.read_bytes()
            if len(data) > _MAX_FILE_BYTES:
                raise SandboxUnavailable("Snapshot guest file exceeds quota")
            if self._baseline.get(rel) != _digest(data):
                changed.append(rel)
        # Removing existing files counts as a change, but this API never
        # automatically deletes the original workspace's files.
        changed.extend(sorted(set(self._baseline) - current))
        return tuple(sorted(changed))

    def export(
        self, relative: str, *, authorize: Callable[[str, str], bool],
    ) -> bytes:
        """Return approved guest bytes; NEVER auto-write into the source root.

        The trusted app can show diffs and apply using its own server-side
        owner/action policy and atomic file API. Deleted files are not exported.
        """
        parts = _safe_parts(relative)
        changed = self.changed()
        if relative not in changed:
            raise PermissionError("Only changed guest files may be exported")
        if not callable(authorize) or authorize(relative, "export-guest-file") is not True:
            raise PermissionError("Trusted exact-file export approval required")
        path = self.workspace.joinpath(*parts)
        if not path.is_file() or path.is_symlink():
            raise PermissionError("Cannot export deleted or symlinked guest object")
        data = path.read_bytes()
        if len(data) > _MAX_FILE_BYTES:
            raise SandboxUnavailable("Guest file exceeds export bound")
        # Refuse exporting into a workspace where the source diverged from
        # the snapshot baseline. A separate application write API must still
        # guard final write races.
        source = self.source.joinpath(*parts)
        if any(p.is_symlink() for p in (self.source.joinpath(*parts[:i]) for i in range(1, len(parts) + 1))):
            raise PermissionError("Source path has become a symlink")
        previous = self._baseline.get(relative)
        if previous is not None:
            if not source.is_file() or _digest(source.read_bytes()) != previous:
                raise PermissionError("Source changed since snapshot creation")
        elif source.exists():
            raise PermissionError("New guest path conflicts with a host path")
        return data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
