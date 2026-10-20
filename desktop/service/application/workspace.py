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
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from core.runtime.session_controller import SessionController
from .attachment_store import AttachmentGrantStore, AttachmentQuotaExceeded
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
        self.quarantine_base = Path(data_dir) / "upload-quarantine"
        # Durable hashed credentials survive local API restarts and are
        # safe across multiple workers sharing the same local data directory.
        # Hosted deployments must use a tenant-aware object store instead.
        self._tenant_id = "local"
        self._uploads = AttachmentGrantStore(Path(data_dir) / "attachments.sqlite3")

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
        # Only an issued, unexpired credential may designate a model
        # attachment. A path string is never itself an authorization.
        tickets = [
            self._uploads.resolve(ref, self._tenant_id, sid) for ref in paths
        ]
        if any(ticket is None for ticket in tickets):
            raise ForbiddenError("Attachment upload credential missing or expired")
        parts = []
        total = 0
        for ticket in tickets:
            assert ticket is not None
            path, expected_identity = ticket.filename, ticket.identity
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
                final = os.fstat(stream.fileno())
                final_identity = (
                    final.st_dev, final.st_ino, final.st_size,
                    final.st_mtime_ns, final.st_ctime_ns,
                )
                if len(data) != length or final_identity != expected_identity:
                    raise ForbiddenError("Uploaded file changed during reading")
            try:
                parts.append(make_attachment_part(path, data))
            except ValueError as exc:
                raise InvalidInputError(str(exc)) from exc
        return parts

    @contextmanager
    def _existing_upload_root_fd(self, sid: str) -> Iterator[int]:
        """Open an existing persisted session root without re-creating it.

        Cleanup runs after server restart, before old sessions are activated.
        The descriptor walk is rooted at our own storage base.
        """
        if not _SESSION_ID.fullmatch(sid) or not _SECURE_OPEN:
            raise ForbiddenError("Invalid or unsupported upload root")
        try:
            base_fd = os.open(self.workspace_base, _DIR_FLAGS)
        except FileNotFoundError as exc:
            raise NotFoundError("Session upload root missing") from exc
        try:
            fd = os.open(sid, _DIR_FLAGS, dir_fd=base_fd)
            try:
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(base_fd)

    @contextmanager
    def _quarantine_directory_fd(self, sid: str) -> Iterator[int]:
        """Stage untrusted uploads outside the Agent-visible session root."""
        self._validate_session(sid)
        if not _SECURE_OPEN:
            raise ForbiddenError("Secure upload quarantine unavailable")
        self.quarantine_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_fd = os.open(self.quarantine_base, _DIR_FLAGS)
        try:
            try:
                os.mkdir(sid, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child = os.open(sid, _DIR_FLAGS, dir_fd=parent_fd)
            try:
                yield child
            finally:
                os.close(child)
        finally:
            os.close(parent_fd)

    def cleanup_orphan_quarantine(self, *, now: float | None = None) -> int:
        """Remove old uncommitted upload stages without following symlinks."""
        import re as _re
        import time as _time
        current = _time.time() if now is None else now
        if not _SECURE_OPEN:
            return 0
        try:
            root_fd = os.open(self.quarantine_base, _DIR_FLAGS)
        except FileNotFoundError:
            return 0
        removed = 0
        try:
            for sid in os.listdir(root_fd):
                if not _SESSION_ID.fullmatch(sid):
                    continue
                try:
                    dir_fd = os.open(sid, _DIR_FLAGS, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    for name in os.listdir(dir_fd):
                        if not _re.fullmatch(r"\\.ridge-incoming-[0-9a-f]{24}", name):
                            continue
                        try:
                            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                            if (stat.S_ISREG(info.st_mode)
                                    and current - info.st_mtime >= 7200):
                                os.unlink(name, dir_fd=dir_fd)
                                removed += 1
                        except OSError:
                            continue
                finally:
                    os.close(dir_fd)
        finally:
            os.close(root_fd)
        return removed

    def cleanup_expired_uploads(self, *, now: float | None = None) -> dict:
        """Revoke expired grants and unlink ONLY unchanged session upload files.

        Unlinking is confined by the session root fd and the exact identity
        saved when uploaded. A user-created replacement or symlink is never
        removed. Caller should schedule this periodically for idle servers.
        """
        removed = 0
        pending = 0
        candidates = self._uploads.expired_candidates(now=now)
        for digest, grant in candidates:
            if grant.tenant_id != self._tenant_id:
                continue
            acknowledged = False
            try:
                with self._existing_upload_root_fd(grant.session_id) as parent_fd:
                    try:
                        latest = os.stat(
                            grant.filename, dir_fd=parent_fd, follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        acknowledged = True  # already deleted by user/another GC
                    else:
                        identity = (
                            latest.st_dev, latest.st_ino, latest.st_size,
                            latest.st_mtime_ns, latest.st_ctime_ns,
                        )
                        if stat.S_ISREG(latest.st_mode) and identity == grant.identity:
                            os.unlink(grant.filename, dir_fd=parent_fd)
                            removed += 1
                        # File was replaced by the user or is no longer a
                        # regular file: never delete their replacement.
                        acknowledged = True
            except (FileNotFoundError, NotFoundError):
                acknowledged = True
            except (OSError, ForbiddenError):
                # Keep the expired DB row to retry after transient IO or
                # permission failures rather than leaking files forever.
                pending += 1
            if acknowledged:
                self._uploads.acknowledge_expired(digest, now=now)
        return {
            "expired_grants": len(candidates), "files_removed": removed,
            "retry_pending": pending,
            "quarantine_files_removed": self.cleanup_orphan_quarantine(now=now),
        }

    def revoke_session_uploads(self, sid: str) -> None:
        self._uploads.revoke_session(self._tenant_id, sid)

    def upload(self, sid: str, filename: str, source: BinaryIO) -> dict:
        """Stage privately, scan the immutable fd, THEN publish to workspace.

        The Agent's /workspace mount never sees a file which has not passed
        the configured scanner. No paths/bytes supplied by the client are
        passed to a host command interpreter.
        """
        from .attachment_scanner import scan_uploaded_fd, UploadRejected

        name = Path((filename or "upload").replace("\\", "/")).name
        if (not name or name in {".", ".."} or "\x00" in name
                or len(name.encode("utf-8")) > 240):
            raise InvalidInputError("Invalid upload filename")
        stem, suffix = Path(name).stem, Path(name).suffix
        stage_name = ".ridge-incoming-" + secrets.token_hex(12)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
        with (self._root_fd(sid) as root_fd,
              self._quarantine_directory_fd(sid) as quarantine_fd):
            published: str | None = None
            published_inode: tuple[int, int] | None = None
            committed = False
            try:
                fd = os.open(stage_name, flags, 0o600, dir_fd=quarantine_fd)
                size = 0
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        chunk = source.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise PayloadTooLargeError("Upload exceeds 25 MiB limit")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())

                scan_fd = os.open(
                    stage_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=quarantine_fd,
                )
                try:
                    initial = os.fstat(scan_fd)
                    if not stat.S_ISREG(initial.st_mode):
                        raise UploadRejected("Staged upload is not a regular file")
                    scan_uploaded_fd(scan_fd)
                    verified = os.fstat(scan_fd)
                    if (initial.st_dev, initial.st_ino, initial.st_size,
                            initial.st_mtime_ns, initial.st_ctime_ns) != (
                            verified.st_dev, verified.st_ino, verified.st_size,
                            verified.st_mtime_ns, verified.st_ctime_ns):
                        raise UploadRejected("Upload changed during scan")
                finally:
                    os.close(scan_fd)

                # A hard link performs exclusive publication without any
                # rename-overwrite race, only after antivirus succeeds.
                for index in range(1000):
                    candidate = name if index == 0 else f"{stem}_{index}{suffix}"
                    try:
                        os.link(
                            stage_name, candidate, src_dir_fd=quarantine_fd,
                            dst_dir_fd=root_fd, follow_symlinks=False,
                        )
                        published = candidate
                        published_inode = (initial.st_dev, initial.st_ino)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise InvalidInputError("Too many duplicate upload filenames")
                # Removing the staging hardlink changes inode ctime; record
                # the final post-publish ctime, not the pre-scan timestamp.
                os.unlink(stage_name, dir_fd=quarantine_fd)
                live = os.stat(published, dir_fd=root_fd, follow_symlinks=False)
                if (not stat.S_ISREG(live.st_mode)
                        or (live.st_dev, live.st_ino) != published_inode
                        or live.st_size != size
                        or live.st_mtime_ns != initial.st_mtime_ns):
                    raise UploadRejected("Published file changed during admission")
                identity = (live.st_dev, live.st_ino, live.st_size,
                            live.st_mtime_ns, live.st_ctime_ns)
                credential = secrets.token_urlsafe(32)
                self._uploads.issue(
                    credential, self._tenant_id, sid, published, identity,
                )
                committed = True
                return {
                    "path": published, "name": name, "size": size,
                    "attachment_ref": credential,
                }
            except AttachmentQuotaExceeded as exc:
                raise PayloadTooLargeError("Attachment quota exceeded") from exc
            except UploadRejected as exc:
                raise ForbiddenError("Upload did not pass required scan") from exc
            finally:
                try:
                    os.unlink(stage_name, dir_fd=quarantine_fd)
                except FileNotFoundError:
                    pass
                if not committed and published is not None:
                    try:
                        info = os.stat(
                            published, dir_fd=root_fd, follow_symlinks=False,
                        )
                        if (info.st_dev, info.st_ino) == published_inode:
                            os.unlink(published, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass
