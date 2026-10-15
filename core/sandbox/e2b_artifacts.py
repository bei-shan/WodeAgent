"""Explicitly approved, no-clobber E2B artifact publication into a host project.

Remote cloud file data is untrusted. This module is application-only: NO Agent
tool, model argument, remote VM metadata, or plugin may supply the approval
callback. A trusted UI/service must obtain a fresh human approval separately.

Writes are restricted to existing host 'artifacts/' subdirectories and use
dirfd/O_NOFOLLOW + atomic hardlink publication. Never follow symlinks, create
folders, overwrite files, touch dotfiles or expose remote VM secret paths.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.e2b_guest_files import MAX_REMOTE_FILE_BYTES


class RemoteArtifactReader(Protocol):
    def read_file(self, path: str, *, timeout_seconds: float = 30) -> bytes: ...


# source (guest path), destination (project-relative path), size, SHA-256
Approval = Callable[[str, str, int, str], bool]


@dataclass(frozen=True)
class PublishedArtifact:
    relative_path: str
    bytes_written: int
    sha256: str


def _target_components(destination: str) -> list[str]:
    if (not isinstance(destination, str) or not destination
            or len(destination) > 1024 or destination.startswith("/")):
        raise PermissionError("Artifact destination must be a relative path")
    if any(ord(c) < 32 or ord(c) == 127 for c in destination) or "\\" in destination:
        raise PermissionError("Invalid artifact destination")
    names = destination.split("/")
    if (not 2 <= len(names) <= 16 or names[0] != "artifacts"
            or any(not p or p in (".", "..") or p.startswith(".")
                   or len(p.encode("utf-8")) > 255 for p in names)):
        raise PermissionError("Artifact target is outside the dedicated artifacts tree")
    return names


def publish_e2b_artifact(
    *,
    workspace: str | Path,
    reader: RemoteArtifactReader,
    remote_path: str,
    destination: str,
    approve: Approval | None,
) -> PublishedArtifact:
    """Read a small remote file into memory and publish only after approval.

    'approve' must be bound to an authenticated human consent action by an
    application service. This function cannot itself authenticate a user and
    must never be directly wired as an Agent tool.
    """
    names = _target_components(destination)
    if not callable(approve):
        raise PermissionError("Explicit artifact approval is required")
    root = Path(workspace)
    if not root.is_dir():
        raise SandboxUnavailable("Artifact project root unavailable")
    data = reader.read_file(remote_path, timeout_seconds=30)
    if not isinstance(data, bytes) or len(data) > MAX_REMOTE_FILE_BYTES:
        raise SandboxUnavailable("Remote artifact exceeds the transfer budget")
    digest = hashlib.sha256(data).hexdigest()
    try:
        permitted = approve(remote_path, destination, len(data), digest)
    except Exception:
        raise PermissionError("Artifact authorization failed closed") from None
    if permitted is not True:
        raise PermissionError("Artifact was not explicitly approved")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(root, flags)
    tmp = ".ridge-artifact-" + secrets.token_hex(16)
    created = False
    try:
        for name in names[:-1]:
            next_fd = os.open(name, flags, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent,
        )
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-overwrite: unlike os.replace, link raises EEXIST for
        # existing files, hardlinks and symlinks at the destination.
        os.link(
            tmp, names[-1],
            src_dir_fd=parent, dst_dir_fd=parent,
            follow_symlinks=False,
        )
        os.fsync(parent)
    except (OSError, ValueError) as exc:
        raise SandboxUnavailable(
            "Artifact publication denied, destination unavailable or already exists"
        ) from None
    finally:
        if created:
            try:
                os.unlink(tmp, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)
    return PublishedArtifact(destination, len(data), digest)
