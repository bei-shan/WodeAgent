"""Session-scoped file operations, independent from FastAPI and presentation.

The session workspace is the only readable/writable root. All paths are
resolved and validated before access; symlinks outside the workspace are
rejected, and a file upload never overwrites an existing path.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import BinaryIO

from core.runtime.session_controller import SessionController
from .errors import (
    ForbiddenError, InvalidInputError, NotFoundError, PayloadTooLargeError,
)

_SESSION_ID = re.compile(r"[0-9a-f]{12}\Z")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024


class WorkspaceService:
    """File use cases scoped to one active AgentSession.

    HTTP adapters own their status codes and response streaming. The service
    returns framework-neutral dictionaries or an already-open file handle.
    """

    def __init__(self, controller: SessionController, data_dir: str | Path):
        self.controller = controller
        self.workspace_base = Path(data_dir) / "sessions"

    def root(self, sid: str) -> Path:
        if not _SESSION_ID.fullmatch(sid):
            raise NotFoundError("Session not found")
        session = self.controller.get_session(sid)
        if session is None:
            raise NotFoundError("Session not found")
        if session.workspace_dir:
            # This location is assigned by the trusted Agent runtime.
            return Path(session.workspace_dir).resolve()
        base = self.workspace_base.resolve()
        candidate = base / sid
        candidate.mkdir(parents=True, exist_ok=True)
        root = candidate.resolve()
        try:
            root.relative_to(base)
        except ValueError as exc:
            raise ForbiddenError("Session workspace is outside the workspace base") from exc
        return root

    def _target(self, sid: str, path: str) -> tuple[Path, Path]:
        root = self.root(sid)
        try:
            target = (root / path).resolve()
            target.relative_to(root)
        except (ValueError, RuntimeError, OSError) as exc:
            raise ForbiddenError("Path is outside session root or invalid") from exc
        return root, target

    def list(self, sid: str, path: str = ".", limit: int = 200) -> dict:
        if not 1 <= limit <= 500:
            raise InvalidInputError("Invalid directory listing limit")
        root, target = self._target(sid, path)
        if not target.exists():
            raise NotFoundError(f"Path not found: {path}")
        if not target.is_dir():
            raise InvalidInputError("Path is not a directory")
        try:
            items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError as exc:
            raise ForbiddenError("Permission denied") from exc
        entries: list[dict[str, str]] = []
        for item in items[:limit]:
            try:
                resolved = item.resolve()
                resolved.relative_to(root)
                # Never advertise an entry whose link leads outside this session.
                if item.is_symlink():
                    continue
                rel = str(item.relative_to(root)).replace("\\", "/")
                entries.append({
                    "name": item.name, "path": rel,
                    "type": "directory" if item.is_dir() else "file",
                })
            except (ValueError, OSError, RuntimeError):
                continue
        return {"path": path, "entries": entries, "truncated": len(items) > limit}

    def content(self, sid: str, path: str, start_line: int = 1, limit: int = 500) -> dict:
        if start_line < 1 or not 1 <= limit <= 2000:
            raise InvalidInputError("Invalid line range")
        _, target = self._target(sid, path)
        if not target.exists():
            raise NotFoundError(f"File not found: {path}")
        if target.is_dir():
            raise InvalidInputError("Path is a directory, not a file")
        try:
            if target.stat().st_size > MAX_PREVIEW_BYTES:
                raise PayloadTooLargeError("File too large to preview")
            # Read only the preview-sized file; binary/invalid UTF-8 fails closed.
            with target.open("r", encoding="utf-8") as stream:
                text = stream.read(MAX_PREVIEW_BYTES + 1)
            if len(text) > MAX_PREVIEW_BYTES or "\x00" in text:
                raise InvalidInputError("File appears to be binary or too large")
        except UnicodeDecodeError as exc:
            raise InvalidInputError("File appears to be binary") from exc
        except PermissionError as exc:
            raise ForbiddenError("Permission denied") from exc
        lines = text.splitlines()
        start_idx = start_line - 1
        end_idx = min(start_idx + limit, len(lines))
        selected = lines[start_idx:end_idx]
        numbered = "\n".join(
            f"{i + 1}\t{line}" for i, line in enumerate(selected, start=start_idx)
        )
        return {"path": path, "content": numbered, "truncated": end_idx < len(lines)}

    def download(self, sid: str, path: str) -> tuple[str, BinaryIO]:
        """Open and return file stream for the adapter to close after sending."""
        _, target = self._target(sid, path)
        if not target.is_file():
            raise NotFoundError(f"File not found: {path}")
        # Prevent a final-component symlink swap after resolution on POSIX.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags)
            return target.name, os.fdopen(fd, "rb")
        except PermissionError as exc:
            raise ForbiddenError("Permission denied") from exc
        except OSError as exc:
            raise ForbiddenError("Unable to open file securely") from exc

    def upload(self, sid: str, filename: str, source: BinaryIO) -> dict:
        root = self.root(sid)
        # Normalize both separator styles to defeat Windows-style traversal.
        name = Path((filename or "upload").replace("\\", "/")).name
        if not name or name in {".", ".."} or "\x00" in name or len(name.encode("utf-8")) > 240:
            raise InvalidInputError("Invalid upload filename")
        stem, suffix = Path(name).stem, Path(name).suffix
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        for index in range(1000):
            target = root / (name if index == 0 else f"{stem}_{index}{suffix}")
            try:
                fd = os.open(target, flags, 0o600)
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
            target.unlink(missing_ok=True)
            raise
        return {"path": target.name, "name": name, "size": size}
