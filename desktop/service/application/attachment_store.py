"""Durable, tenant-scoped local attachment grants with bounded retention.

SQLite is the single-host storage backend, not distributed object storage.
Opaque tokens are stored as SHA-256 hashes and bound to tenant, session,
filename, inode, device and nanosecond file metadata. Transactions serialize
quota admission across worker processes; expired rows can be leased for
safe, identity-checked cleanup by the workspace service.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple


class AttachmentQuotaExceeded(ValueError):
    pass


class AttachmentGrant(NamedTuple):
    tenant_id: str
    session_id: str
    filename: str
    identity: tuple[int, int, int, int, int]
    issued_at: float
    expires_at: float


class AttachmentGrantStore:
    TTL_SECONDS = 7200
    MAX_SESSION_UPLOADS = 128
    MAX_SESSION_BYTES = 100 * 1024 * 1024
    MAX_TOTAL_BYTES = 1024 * 1024 * 1024
    _TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

    def __init__(self, file: str | Path):
        self.path = Path(file).absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS attachment_grants (
                    token_hash TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_attachment_expiry ON attachment_grants(expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attachment_owner ON attachment_grants(tenant_id, session_id)")
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @classmethod
    def _owner(cls, tenant_id: str, session_id: str) -> None:
        if (not isinstance(tenant_id, str) or not cls._TENANT.fullmatch(tenant_id)
                or not isinstance(session_id, str)
                or not re.fullmatch(r"[0-9a-f]{12}", session_id)):
            raise ValueError("Invalid attachment tenant/session scope")

    @staticmethod
    def _hash(token: str) -> str:
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise ValueError("Invalid attachment credential")
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(
        self, token: str, tenant_id: str, session_id: str,
        filename: str, identity: tuple[int, int, int, int, int],
        *, now: float | None = None,
    ) -> None:
        self._owner(tenant_id, session_id)
        digest = self._hash(token)
        if (not filename or "/" in filename or "\\" in filename
                or filename in {".", ".."}):
            raise ValueError("Invalid attachment filename")
        current = time.time() if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            n, amount = db.execute(
                """SELECT COUNT(*), COALESCE(SUM(byte_size), 0)
                   FROM attachment_grants
                   WHERE tenant_id=? AND session_id=? AND expires_at>?""",
                (tenant_id, session_id, current),
            ).fetchone()
            total = db.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM attachment_grants WHERE expires_at>?",
                (current,),
            ).fetchone()[0]
            if (n >= self.MAX_SESSION_UPLOADS
                    or amount + identity[2] > self.MAX_SESSION_BYTES
                    or total + identity[2] > self.MAX_TOTAL_BYTES):
                raise AttachmentQuotaExceeded("Attachment storage quota exceeded")
            db.execute(
                """INSERT INTO attachment_grants
                   (token_hash, tenant_id, session_id, filename, device,
                    inode, byte_size, mtime_ns, ctime_ns, issued_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (digest, tenant_id, session_id, filename, *identity,
                 current, current + self.TTL_SECONDS),
            )

    def resolve(
        self, token: str, tenant_id: str, session_id: str,
        *, now: float | None = None,
    ) -> AttachmentGrant | None:
        self._owner(tenant_id, session_id)
        try:
            digest = self._hash(token)
        except (ValueError, UnicodeEncodeError):
            return None
        current = time.time() if now is None else now
        with self._connect() as db:
            row = db.execute(
                """SELECT tenant_id, session_id, filename, device, inode,
                          byte_size, mtime_ns, ctime_ns, issued_at, expires_at
                   FROM attachment_grants
                   WHERE token_hash=? AND tenant_id=? AND session_id=? AND expires_at>?""",
                (digest, tenant_id, session_id, current),
            ).fetchone()
        if not row:
            return None
        return AttachmentGrant(
            row[0], row[1], row[2],
            tuple(int(v) for v in row[3:8]), row[8], row[9],
        )

    def expired_candidates(
        self, *, now: float | None = None,
    ) -> list[tuple[str, AttachmentGrant]]:
        """List expired grants without revoking the cleanup retry record.

        A failed filesystem unlink must not silently lose the only durable
        reference to an orphaned upload. Multiple GC workers may see the same
        candidate; deletion is identity checked by WorkspaceService.
        """
        current = time.time() if now is None else now
        with self._connect() as db:
            rows = db.execute(
                """SELECT token_hash, tenant_id, session_id, filename, device,
                          inode, byte_size, mtime_ns, ctime_ns, issued_at,
                          expires_at
                   FROM attachment_grants WHERE expires_at<=?""",
                (current,),
            ).fetchall()
        return [
            (row[0], AttachmentGrant(
                row[1], row[2], row[3], tuple(int(v) for v in row[4:9]),
                row[9], row[10],
            )) for row in rows
        ]

    def acknowledge_expired(
        self, token_hash: str, *, now: float | None = None,
    ) -> None:
        """Remove only already-expired grants, never reactivate or move them."""
        current = time.time() if now is None else now
        with self._connect() as db:
            db.execute(
                "DELETE FROM attachment_grants WHERE token_hash=? AND expires_at<=?",
                (token_hash, current),
            )

    def expire(self, *, now: float | None = None) -> list[AttachmentGrant]:
        """Atomically revoke expired tickets, returning safe unlink candidates."""
        current = time.time() if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT tenant_id, session_id, filename, device, inode,
                          byte_size, mtime_ns, ctime_ns, issued_at, expires_at
                   FROM attachment_grants WHERE expires_at<=?""",
                (current,),
            ).fetchall()
            db.execute("DELETE FROM attachment_grants WHERE expires_at<=?", (current,))
        return [
            AttachmentGrant(row[0], row[1], row[2],
                            tuple(int(v) for v in row[3:8]), row[8], row[9])
            for row in rows
        ]

    def revoke_session(self, tenant_id: str, session_id: str) -> None:
        self._owner(tenant_id, session_id)
        with self._connect() as db:
            db.execute(
                "DELETE FROM attachment_grants WHERE tenant_id=? AND session_id=?",
                (tenant_id, session_id),
            )

    def stats(self, tenant_id: str, session_id: str) -> tuple[int, int]:
        self._owner(tenant_id, session_id)
        with self._connect() as db:
            n, size = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_size),0) FROM attachment_grants WHERE tenant_id=? AND session_id=? AND expires_at>?",
                (tenant_id, session_id, time.time()),
            ).fetchone()
        return int(n), int(size)
