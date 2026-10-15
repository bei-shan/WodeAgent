"""Optional PostgreSQL storage for private, tenant/project/user-scoped sessions.

NOT connected to the current local Web routes: adding a shared session DB
without verified OIDC principals and resource-level ACL would be insecure.
Every operation opens a transaction and installs RLS scope with SET LOCAL.
Requires psycopg>=3 (optional requirements-postgres.txt).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from core.memory.contracts import MemoryScope

_SESSION_ID = re.compile(r"[0-9a-f]{12}\Z")


class SessionVersionConflict(RuntimeError):
    """The session changed, vanished, or is outside the authorized scope."""


@dataclass(frozen=True, slots=True)
class StoredPrivateSession:
    id: str
    title: str
    snapshot: dict[str, Any]
    version: int


class PostgresPrivateSessionStore:
    """Standalone adapter; do not expose until an authenticated principal exists.

    The DSN must target a runtime role that is NOT SUPERUSER/BYPASSRLS and
    does not own an unprotected table. Migrations are separate and privileged.
    """

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("A PostgreSQL DSN is required")
        self._dsn = dsn

    @staticmethod
    def _validate(scope: MemoryScope, session_id: str) -> None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated server-derived MemoryScope is required")
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid 12-character session ID")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install requirements-postgres.txt to use PostgreSQL") from exc
        return psycopg.connect(self._dsn, autocommit=False)

    @staticmethod
    def _scope(cur, scope: MemoryScope) -> None:
        # Transaction-local settings never survive checkout/reuse of a pooled
        # connection. Parameters are bound, not interpolated into SQL strings.
        cur.execute(
            """SELECT set_config('ridge.tenant_id', %s, true),
                      set_config('ridge.user_id', %s, true),
                      set_config('ridge.project_id', %s, true)""",
            (scope.tenant_id, scope.user_id, scope.project_id),
        )

    def create(
        self, scope: MemoryScope, session_id: str, *,
        title: str = "", snapshot: Mapping[str, Any] | None = None,
    ) -> StoredPrivateSession:
        self._validate(scope, session_id)
        if not isinstance(title, str) or len(title) > 256:
            raise ValueError("Invalid session title")
        encoded = json.dumps(dict(snapshot or {}), allow_nan=False, ensure_ascii=False)
        if len(encoded) > 4_000_000:
            raise ValueError("Session snapshot exceeds 4 MB")
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """INSERT INTO ridge.private_sessions
                        (tenant_id, project_id, owner_user_id, session_id, title, snapshot)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                       ON CONFLICT DO NOTHING RETURNING version""",
                    (scope.tenant_id, scope.project_id, scope.user_id,
                     session_id, title, encoded),
                )
                row = cur.fetchone()
                if row is None:
                    raise SessionVersionConflict("Session already exists in this scope")
                return StoredPrivateSession(session_id, title, json.loads(encoded), int(row[0]))

    def get(self, scope: MemoryScope, session_id: str) -> StoredPrivateSession | None:
        self._validate(scope, session_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """SELECT title, snapshot, version FROM ridge.private_sessions
                       WHERE tenant_id=%s AND project_id=%s
                         AND owner_user_id=%s AND session_id=%s""",
                    (scope.tenant_id, scope.project_id, scope.user_id, session_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                raw = row[1]
                return StoredPrivateSession(
                    session_id, row[0], raw if isinstance(raw, dict) else json.loads(raw),
                    int(row[2]),
                )

    def list_private(self, scope: MemoryScope, *, limit: int = 50) -> list[StoredPrivateSession]:
        """Return only scoped, privately owned sessions with bounded pagination."""
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated server-derived MemoryScope required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """SELECT session_id, title, snapshot, version
                       FROM ridge.private_sessions
                       WHERE tenant_id=%s AND project_id=%s AND owner_user_id=%s
                       ORDER BY updated_at DESC, session_id DESC LIMIT %s""",
                    (scope.tenant_id, scope.project_id, scope.user_id, limit),
                )
                return [
                    StoredPrivateSession(
                        str(sid).strip(), title,
                        snap if isinstance(snap, dict) else json.loads(snap),
                        int(version),
                    )
                    for sid, title, snap, version in cur.fetchall()
                ]

    def save(
        self, scope: MemoryScope, session_id: str,
        snapshot: Mapping[str, Any], *, expected_version: int,
        title: str | None = None,
    ) -> int:
        self._validate(scope, session_id)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        if not isinstance(snapshot, Mapping):
            raise TypeError("Session snapshot must be an object")
        if title is not None and (not isinstance(title, str) or len(title) > 256):
            raise ValueError("Invalid session title")
        encoded = json.dumps(dict(snapshot), allow_nan=False, ensure_ascii=False)
        if len(encoded) > 4_000_000:
            raise ValueError("Session snapshot exceeds 4 MB")
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """UPDATE ridge.private_sessions
                       SET snapshot=%s::jsonb,
                           title=COALESCE(%s, title),
                           version=version+1, updated_at=now()
                       WHERE tenant_id=%s AND project_id=%s AND owner_user_id=%s
                         AND session_id=%s AND version=%s
                       RETURNING version""",
                    (encoded, title, scope.tenant_id, scope.project_id,
                     scope.user_id, session_id, expected_version),
                )
                row = cur.fetchone()
                if row is None:
                    raise SessionVersionConflict("Session not available or version changed")
                return int(row[0])

    def delete(self, scope: MemoryScope, session_id: str) -> bool:
        self._validate(scope, session_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """DELETE FROM ridge.private_sessions
                       WHERE tenant_id=%s AND project_id=%s
                         AND owner_user_id=%s AND session_id=%s""",
                    (scope.tenant_id, scope.project_id, scope.user_id, session_id),
                )
                return cur.rowcount == 1
