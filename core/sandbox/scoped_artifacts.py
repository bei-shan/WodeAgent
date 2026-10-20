"""Private, owner-scoped binary artifacts for isolated Agent sessions.

Server-side callers MUST derive MemoryScope from authenticated identity and
the session identifier from the owned session, never from model/URL arguments.
No public HTTP route is registered by this module. The blob directory is
separate from the operator's mutable workspace; uploaded files are NEVER
executable or automatically mounted in a guest.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.memory.contracts import MemoryScope

_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")
_MIMES = frozenset({
    "application/octet-stream", "application/pdf", "application/json",
    "image/png", "image/jpeg", "image/webp", "text/plain", "text/csv",
})
_MAX_BLOB = 2 * 1024 * 1024
_MAX_SESSION_BYTES = 16 * 1024 * 1024
_MAX_SESSION_ITEMS = 32


class ArtifactAccessDenied(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    byte_size: int
    media_type: str
    expires_at: int


class ScopedArtifactStore:
    """Private per-owner storage with persistent metadata, fail-closed reads.

    Single-node SQLite + private local disk only; multi-node deployment needs
    a transactional object store and server-side principal authorization.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ArtifactAccessDenied("Artifact storage root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.root.resolve(strict=True)
        if os.name != "posix":
            raise OSError("Scoped local artifact store requires POSIX no-follow file semantics")
        self.root.chmod(0o700)
        blobs = self.root / "blobs"
        if blobs.is_symlink():
            raise ArtifactAccessDenied("Artifact blob directory must not be a symlink")
        blobs.mkdir(exist_ok=True, mode=0o700)
        blobs.chmod(0o700)
        self.blobs = blobs
        self._lock = threading.RLock()
        self._database = self.root / "artifacts.sqlite3"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
              principal TEXT NOT NULL, project TEXT NOT NULL,
              agent TEXT NOT NULL, session TEXT NOT NULL,
              size INTEGER NOT NULL, media_type TEXT NOT NULL,
              expires_at INTEGER NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_artifact_owner
              ON artifacts(tenant, principal, project, agent, session)""")

    def _db(self):
        return sqlite3.connect(str(self._database), timeout=5)

    @staticmethod
    def _owner(scope: MemoryScope, session_id: str) -> tuple[str, ...]:
        if type(scope) is not MemoryScope or not isinstance(session_id, str) or not _IDENTIFIER.fullmatch(session_id):
            raise ArtifactAccessDenied("Verified owner and valid session id required")
        return (scope.tenant_id, scope.user_id, scope.project_id, scope.agent_id, session_id)

    def _open_blob(self, artifact_id: str, flags: int, mode: int = 0o600) -> int:
        if not isinstance(artifact_id, str) or not re.fullmatch("[0-9a-f]{32}", artifact_id):
            raise ArtifactAccessDenied("Invalid artifact identifier")
        rootfd = os.open(self.blobs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(artifact_id, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=rootfd)
        finally:
            os.close(rootfd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ArtifactAccessDenied("Unsafe artifact blob object")
        return fd

    def put(
        self, scope: MemoryScope, session_id: str, payload: bytes,
        *, media_type: str = "application/octet-stream", ttl_seconds: int = 3600,
    ) -> Artifact:
        owner = self._owner(scope, session_id)
        if type(payload) is not bytes or not 0 < len(payload) <= _MAX_BLOB:
            raise ValueError("Artifact payload must be 1..2097152 bytes")
        if media_type not in _MIMES:
            raise ValueError("Artifact media type not permitted")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("Artifact TTL must be 1..86400 seconds")
        expires = int(time.time()) + ttl_seconds
        with self._lock:
            with self._db() as db:
                count, size = db.execute(
                    """SELECT count(*), coalesce(sum(size),0) FROM artifacts WHERE
                     tenant=? AND principal=? AND project=? AND agent=?
                     AND session=? AND expires_at>?""", (*owner, int(time.time())),
                ).fetchone()
                if count >= _MAX_SESSION_ITEMS or size + len(payload) > _MAX_SESSION_BYTES:
                    raise ValueError("Artifact session quota exceeded")
                artifact_id = secrets.token_hex(16)
                fd = self._open_blob(artifact_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    with os.fdopen(fd, "wb") as target:
                        target.write(payload)
                        target.flush()
                        os.fsync(target.fileno())
                    db.execute(
                        """INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)""",
                        (artifact_id, *owner, len(payload), media_type, expires),
                    )
                except BaseException:
                    try:
                        (self.blobs / artifact_id).unlink()
                    except FileNotFoundError:
                        pass
                    raise
        return Artifact(artifact_id, len(payload), media_type, expires)

    def _owned_row(self, db, owner: tuple[str, ...], artifact_id: str):
        if not isinstance(artifact_id, str) or not re.fullmatch("[0-9a-f]{32}", artifact_id):
            raise ArtifactAccessDenied("Unknown or unauthorized artifact")
        row = db.execute(
            """SELECT size, media_type, expires_at FROM artifacts
               WHERE artifact_id=? AND tenant=? AND principal=?
               AND project=? AND agent=? AND session=?""",
            (artifact_id, *owner),
        ).fetchone()
        if row is None or row[2] <= int(time.time()):
            raise ArtifactAccessDenied("Unknown or expired artifact")
        return row

    def read(self, scope: MemoryScope, session_id: str, artifact_id: str) -> bytes:
        owner = self._owner(scope, session_id)
        with self._lock:
            with self._db() as db:
                size, _mime, _expiry = self._owned_row(db, owner, artifact_id)
                try:
                    fd = self._open_blob(artifact_id, os.O_RDONLY)
                    with os.fdopen(fd, "rb") as source:
                        if os.fstat(source.fileno()).st_size != size:
                            raise ArtifactAccessDenied("Artifact size changed")
                        data = source.read(size + 1)
                except FileNotFoundError as exc:
                    raise ArtifactAccessDenied("Artifact blob unavailable") from exc
                if len(data) != size:
                    raise ArtifactAccessDenied("Artifact blob size mismatch")
                return data

    def delete(self, scope: MemoryScope, session_id: str, artifact_id: str) -> bool:
        owner = self._owner(scope, session_id)
        with self._lock:
            with self._db() as db:
                self._owned_row(db, owner, artifact_id)
                db.execute(
                    """DELETE FROM artifacts WHERE artifact_id=? AND tenant=? AND
                       principal=? AND project=? AND agent=? AND session=?""",
                    (artifact_id, *owner),
                )
            try:
                (self.blobs / artifact_id).unlink()
            except FileNotFoundError:
                pass
            return True

    def purge_expired(self) -> int:
        """Operator-owned bounded maintenance; no untrusted recursive paths."""
        with self._lock:
            with self._db() as db:
                expired = [r[0] for r in db.execute(
                    "SELECT artifact_id FROM artifacts WHERE expires_at<=?",
                    (int(time.time()),),
                )]
                db.execute("DELETE FROM artifacts WHERE expires_at<=?", (int(time.time()),))
            for identifier in expired:
                try:
                    (self.blobs / identifier).unlink()
                except FileNotFoundError:
                    pass
            return len(expired)
