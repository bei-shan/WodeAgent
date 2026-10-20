"""Descriptor-anchored per-session file operations (no FastAPI dependencies).

All untrusted path components are walked with O_NOFOLLOW using directory file
descriptors. A renamed directory or symlink inserted between checks cannot
redirect any operation outside the session's already opened directory.
"""
from __future__ import annotations

import os
import re
import stat
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from core.runtime.session_controller import SessionController
from .errors import ForbiddenError, InvalidInputError, NotFoundError, PayloadTooLargeError

_SESSION_ID = re.compile(r"[0-9a-f]{12}\Z")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024

# A safe local filesystem transport must fail closed rather than fall back to
# resolve-then-open when the host lacks descriptor-relative secure traversal.
_SECURE_OPEN = hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class WorkspaceService:
    """Session-scoped workspace operations; the filesystem is not trusted."""

    def __init__(self, controller: SessionController, data_dir: str | Path):
        self.controller = controller
        self.workspace_base = Path(data_dir) / "sessions"
        # Random, short-lived upload capabilities. Never grant arbitrary
        # workspace paths as attachments; uploaded files may be unrelated
        # to Agent-generated private/config files. Local server owns this
        # registry; distributed hosted mode needs its own durable asset DB.
        self._uploaded: dict[str, dict[str, tuple[str, tuple[int, ...], float]]] = {}
        self._uploaded_lock = threading.RLock()

    def _validate_session(self, sid: str) -> None:
        if not _SESSION_ID.fullmatch(sid) or self.controller.get_session(sid) is None:
            raise NotFoundError("Session not found")

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        # Backslashes are separators on Windows; reject them on POSIX too.
        if not isinstance(path, str) or "\x00" in path or "\\" in path:
            raise ForbiddenError("Invalid workspace path")
        if path.startswith("/"):
            raise ForbiddenError("Path is outside session root")
        parts = tuple(component for component in path.split("/") if component and component != ".")
        if any(part == ".." for part in parts):
            raise ForbiddenError("Path is outside session root")
        return parts

    @contextmanager
    def _root_fd(self, sid: str) -> Iterator[int]:
        self._validate_session(sid)
        if not _SECURE_OPEN:
            raise ForbiddenError("Secure workspace access is unsupported on this host")
        # data_dir is server-configured; the session component itself is
        # untrusted and must be opened relative to an anchored base descriptor.
        self.workspace_base.mkdir(parents=True, exist_ok=True)
        try:
            parent_fd = os.open(self.workspace_base, _DIR_FLAGS)
        except OSError as exc:
            raise ForbiddenError("Cannot access workspace base") from exc
        try:
            try:
                os.mkdir(sid, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                fd = os.open(sid, _DIR_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise ForbiddenError("Unsafe session workspace") from exc
            try:
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    @contextmanager
    def _directory(self, sid: str, path: str) -> Iterator[int]:
        parts = self._parts(path)
        with self._root_fd(sid) as root_fd:
            current = os.dup(root_fd)
            try:
                for component in parts:
                    try:
                        child = os.open(component, _DIR_FLAGS, dir_fd=current)
                    except FileNotFoundError as exc:
                        raise NotFoundError(f"Path not found: {path}") from exc
                    except OSError as exc:
                        raise ForbiddenError("Unsafe directory path") from exc
                    os.close(current)
                    current = child
                yield current
            finally:
                os.close(current)

    @contextmanager
    def _open_file(self, sid: str, path: str) -> Iterator[tuple[str, BinaryIO]]:
        parts = self._parts(path)
        if not parts:
            raise InvalidInputError("Path is a directory, not a file")
        with self._directory(sid, "/".join(parts[:-1])) as directory_fd:
            try:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError as exc:
                raise NotFoundError(f"File not found: {path}") from exc
            except OSError as exc:
                raise ForbiddenError("Unsafe file path") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise InvalidInputError("Path is a directory, not a file")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    yield parts[-1], handle
            finally:
                os.close(fd)

    def list(self, sid: str, path: str = ".", limit: int = 200) -> dict:
        if not 1 <= limit <= 500:
            raise InvalidInputError("Invalid directory listing limit")
        with self._directory(sid, path) as fd:
            try:
                names = os.listdir(fd)
                items = []
                for name in names:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    # Deliberately suppress symlinks and special device files.
                    if stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(info.st_mode):
                        kind = "file"
                    else:
                        continue
                    items.append((name, kind))
            except PermissionError as exc:
                raise ForbiddenError("Permission denied") from exc
            items.sort(key=lambda item: (item[1] == "file", item[0].lower()))
            prefix = "/".join(self._parts(path))
            entries = [
                {"name": name, "path": f"{prefix}/{name}" if prefix else name, "type": kind}
                for name, kind in items[:limit]
            ]
            return {"path": path, "entries": entries, "truncated": len(items) > limit}

    def content(self, sid: str, path: str, start_line: int = 1, limit: int = 500) -> dict:
        if start_line < 1 or not 1 <= limit <= 2000:
            raise InvalidInputError("Invalid line range")
        with self._open_file(sid, path) as (_, handle):
            if os.fstat(handle.fileno()).st_size > MAX_PREVIEW_BYTES:
                raise PayloadTooLargeError("File too large to preview")
            data = handle.read(MAX_PREVIEW_BYTES + 1)
        if len(data) > MAX_PREVIEW_BYTES:
            raise PayloadTooLargeError("File too large to preview")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidInputError("File appears to be binary") from exc
        if "\x00" in text:
            raise InvalidInputError("File appears to be binary")
        lines = text.splitlines()
        start_idx = start_line - 1
        end_idx = min(start_idx + limit, len(lines))
        numbered = "\n".join(
            f"{i + 1}\t{line}" for i, line in enumerate(lines[start_idx:end_idx], start=start_idx)
        )
        return {"path": path, "content": numbered, "truncated": end_idx < len(lines)}

    def download(self, sid: str, path: str) -> tuple[str, BinaryIO]:
        # Transfer ownership of the handle to the HTTP streaming adapter.
        parts = self._parts(path)
        if not parts:
            raise InvalidInputError("Path is a directory, not a file")
        with self._directory(sid, "/".join(parts[:-1])) as directory_fd:
            try:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError as exc:
                raise NotFoundError(f"File not found: {path}") from exc
            except OSError as exc:
                raise ForbiddenError("Unsafe file path") from exc
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise InvalidInputError("Path is a directory, not a file")
            return parts[-1], os.fdopen(fd, "rb")

    def prepare_attachments(self, sid: str, paths: list[str]) -> list[dict]:
        """Bind references to readable files in *this* session, not host paths.

        No arbitrary URL, base64 or client-supplied MIME is trusted. Media bytes
        remain transient in server memory and never enter persisted history.
        """
        from core.runtime.attachment_context import (
            MAX_ATTACHMENTS, MAX_TOTAL_BYTES, make_attachment_part,
        )

        if not isinstance(paths, list) or len(paths) > MAX_ATTACHMENTS:
            raise InvalidInputError("At most four attachments per message")
        if len(paths) != len(set(paths)) or any(
            not isinstance(path, str) or not path or len(path) > 128
            for path in paths
        ):
            raise InvalidInputError("Invalid or repeated attachment reference")
        with self._uploaded_lock:
            session_uploads = self._uploaded.get(sid, {})
            # Always refuse a guessed filename, even if it exists under
            # the session root and would otherwise be readable.
            tickets = [session_uploads.get(ref) for ref in paths]
        if any(ticket is None for ticket in tickets):
            raise ForbiddenError("Attachment upload credential missing or expired")
        parts = []
        total = 0
        for ticket in tickets:
            assert ticket is not None
            path, expected_identity, issued = ticket
            if time.monotonic() - issued > 7200:
                raise ForbiddenError("Attachment upload credential expired")
            # _parts and _open_file enforce descriptor-based per-session root
            # confinement, including O_NOFOLLOW for symlinks.
            components = self._parts(path)
            if not components:
                raise InvalidInputError("Invalid attachment path")
            with self._open_file(sid, path) as (_, stream):
                live = os.fstat(stream.fileno())
                identity = (live.st_dev, live.st_ino, live.st_size,
                            live.st_mtime_ns, live.st_ctime_ns)
                if identity != expected_identity:
                    raise ForbiddenError("Uploaded file changed; upload it again")
                length = live.st_size
                if length > MAX_UPLOAD_BYTES:
                    raise PayloadTooLargeError("Attachment exceeds upload limit")
                total += length
                if total > MAX_TOTAL_BYTES:
                    raise PayloadTooLargeError("Message attachment budget exceeded")
                data = stream.read(length + 1)
                if len(data) != length:
                    raise InvalidInputError("Attachment changed while being read")
            try:
                parts.append(make_attachment_part(path, data))
            except ValueError as exc:
                raise InvalidInputError(str(exc)) from exc
        return parts

    def upload(self, sid: str, filename: str, source: BinaryIO) -> dict:
        name = Path((filename or "upload").replace("\\", "/")).name
        if not name or name in {".", ".."} or "\x00" in name or len(name.encode("utf-8")) > 240:
            raise InvalidInputError("Invalid upload filename")
        stem, suffix = Path(name).stem, Path(name).suffix
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        with self._root_fd(sid) as root_fd:
            for index in range(1000):
                candidate = name if index == 0 else f"{stem}_{index}{suffix}"
                try:
                    fd = os.open(candidate, flags, 0o600, dir_fd=root_fd)
                    break
                except FileExistsError:
                    continue
                except PermissionError as exc:
                    raise ForbiddenError("Permission denied") from exc
            else:
                raise InvalidInputError("Too many duplicate upload filenames")
            size = 0
            try:
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        chunk = source.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise PayloadTooLargeError("Upload exceeds 25 MiB limit")
                        stream.write(chunk)
            except BaseException:
                os.unlink(candidate, dir_fd=root_fd)
                raise
            live = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
            identity = (live.st_dev, live.st_ino, live.st_size,
                        live.st_mtime_ns, live.st_ctime_ns)
            credential = secrets.token_urlsafe(32)
            with self._uploaded_lock:
                registry = self._uploaded.setdefault(sid, {})
                # Bound memory and revoke oldest pending upload grants.
                while len(registry) >= 128:
                    registry.pop(next(iter(registry)))
                registry[credential] = (candidate, identity, time.monotonic())
            return {
                "path": candidate, "name": name, "size": size,
                "attachment_ref": credential,
            }
