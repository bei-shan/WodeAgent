"""Opt-in private SQLite memory store for single-node or development use.

No automatic capture, remote calls, embeddings, or team sharing. SQLite WAL
handles bounded concurrent local writers; it is NOT the production multi-node
backend. Long-running ASGI endpoints must offload calls to a worker.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import MemoryRecord, MemoryScope


class SQLiteMemoryBackend:
    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("A persistent SQLite path is required")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memories_owner
                    ON memories (tenant_id, user_id, project_id, agent_id,
                                 created_at DESC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _check_limit(limit: int) -> int:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Memory search limit must be between 1 and 50")
        return limit

    @staticmethod
    def _row(row: sqlite3.Row, scope: MemoryScope) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], scope=scope, content=row["content"],
            created_at=row["created_at"], metadata=json.loads(row["metadata"]),
        )

    def add(
        self,
        scope: MemoryScope,
        content: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryRecord:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated MemoryScope required")
        if not isinstance(content, str) or not 0 < len(content.strip()) <= 20000:
            raise ValueError("Memory content must be 1..20000 characters")
        payload = json.dumps(dict(metadata or {}), ensure_ascii=False, allow_nan=False)
        if len(payload) > 4096:
            raise ValueError("Memory metadata too large")
        record = MemoryRecord(
            id=secrets.token_hex(16), scope=scope, content=content.strip(),
            created_at=datetime.now(timezone.utc).isoformat(), metadata=dict(metadata or {}),
        )
        with closing(self._connect()) as db:
            with db:
                db.execute(
                    """INSERT INTO memories
                    (id, tenant_id, user_id, project_id, agent_id, content,
                     metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.id, scope.tenant_id, scope.user_id,
                        scope.project_id, scope.agent_id, record.content,
                        payload, record.created_at,
                    ),
                )
        return record

    def search(
        self, scope: MemoryScope, query: str, *, limit: int = 8,
    ) -> list[MemoryRecord]:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated MemoryScope required")
        if not isinstance(query, str) or len(query) > 2048:
            raise ValueError("Invalid memory query")
        limit = self._check_limit(limit)
        # instr() searches literal text: '%' and '_' never become SQL wildcards.
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM memories
                   WHERE tenant_id = ? AND user_id = ? AND project_id = ?
                     AND agent_id = ? AND instr(lower(content), lower(?)) > 0
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (
                    scope.tenant_id, scope.user_id, scope.project_id,
                    scope.agent_id, query, limit,
                ),
            ).fetchall()
        return [self._row(row, scope) for row in rows]

    def delete(self, scope: MemoryScope, record_id: str) -> bool:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated MemoryScope required")
        if not isinstance(record_id, str) or len(record_id) != 32:
            return False
        with closing(self._connect()) as db:
            with db:
                cursor = db.execute(
                    """DELETE FROM memories
                       WHERE id = ? AND tenant_id = ? AND user_id = ?
                         AND project_id = ? AND agent_id = ?""",
                    (
                        record_id, scope.tenant_id, scope.user_id,
                        scope.project_id, scope.agent_id,
                    ),
                )
                return cursor.rowcount == 1
