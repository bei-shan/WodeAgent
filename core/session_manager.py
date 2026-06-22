"""Multi-session manager — create, list, switch, and persist conversation sessions.

Each session is stored as a JSON snapshot under ``memory/sessions/``,
with an ``index.json`` tracking all sessions.

Usage::

    mgr = SessionManager(sessions_dir="memory/sessions")
    session_id = mgr.create_session()
    mgr.save_session(session_id, snapshot_dict)
    mgr.list_sessions()  # [{id, title, created_at, ...}]
    snapshot = mgr.load_session(session_id)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class SessionInfo:
    """Lightweight session metadata stored in index.json."""

    id: str
    title: str
    created_at: str
    modified_at: str
    message_count: int = 0
    preview: str = ""


class SessionManager:
    """Manages multiple conversation sessions with index + snapshot files.

    Parameters
    ----------
    sessions_dir:
        Directory for session storage (default ``memory/sessions``).
    auto_title_max_len:
        Max length for auto-generated titles from first user message.
    """

    INDEX_FILE = "index.json"

    def __init__(
        self,
        sessions_dir: str = "memory/sessions",
        auto_title_max_len: int = 60,
    ):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._auto_title_max_len = auto_title_max_len

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_session(self, title: str = "") -> str:
        """Create a new session and return its ID.

        If *title* is empty, a placeholder title is generated.
        """
        session_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        info = SessionInfo(
            id=session_id,
            title=title or f"New session {now[:16]}",
            created_at=now,
            modified_at=now,
        )
        self._append_index(info)
        # Write an empty snapshot so the file exists.
        self._write_snapshot(session_id, {"version": 1, "history_messages": []})
        return session_id

    def save_session(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """Persist a session snapshot and update the index.

        Automatically extracts *title* from the first user message if
        the current title is a placeholder.
        """
        self._write_snapshot(session_id, snapshot)
        history = snapshot.get("history_messages") or []
        message_count = len(history)
        preview = ""
        title = ""

        # Extract title from first user message.
        for msg in history:
            if msg.get("role") == "user":
                content = str(msg.get("content", "")).strip()
                if content:
                    preview = content[:100]
                    title = content[: self._auto_title_max_len]
                    break

        self._update_index(
            session_id,
            title=title,
            message_count=message_count,
            preview=preview,
            only_overwrite_placeholder=True,
        )

    def load_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Load a session snapshot by ID.  Returns ``None`` if not found."""
        path = self._snapshot_path(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def list_sessions(self) -> list[SessionInfo]:
        """Return all sessions sorted by *modified_at* descending."""
        return sorted(
            self._load_index(),
            key=lambda s: s.modified_at,
            reverse=True,
        )

    def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """Return metadata for a single session, or ``None``."""
        for s in self._load_index():
            if s.id == session_id:
                return s
        return None

    def resolve_identifier(self, identifier: str) -> Optional[str]:
        """Resolve a session identifier to a session ID.

        Supports:
        - Full session ID (12 hex chars)
        - Numeric index in the session list (1-based, as shown in list)
        - Prefix match (min 4 chars)
        """
        identifier = identifier.strip()
        if not identifier:
            return None

        sessions = self.list_sessions()

        # Exact ID match.
        for s in sessions:
            if s.id == identifier:
                return s.id

        # Numeric index (1-based).
        if identifier.isdigit():
            idx = int(identifier) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx].id

        # Prefix match (min 4 chars).
        if len(identifier) >= 4:
            matches = [s for s in sessions if s.id.startswith(identifier)]
            if len(matches) == 1:
                return matches[0].id

        return None

    def rename_session(self, session_id: str, title: str) -> bool:
        """Rename a session. Returns ``True`` on success."""
        index = self._load_index()
        for s in index:
            if s.id == session_id:
                s.title = title.strip()
                self._write_index(index)
                return True
        return False

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (snapshot + index entry). Returns ``True`` on success."""
        # Remove snapshot file.
        snap = self._snapshot_path(session_id)
        if snap.exists():
            try:
                snap.unlink()
            except OSError:
                return False

        # Remove from index.
        index = self._load_index()
        new_index = [s for s in index if s.id != session_id]
        if len(new_index) == len(index):
            return False  # not found
        self._write_index(new_index)
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _snapshot_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _index_path(self) -> Path:
        return self._dir / self.INDEX_FILE

    def _load_index(self) -> list[SessionInfo]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        result: list[SessionInfo] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            result.append(SessionInfo(
                id=str(item.get("id", "")),
                title=str(item.get("title", "")),
                created_at=str(item.get("created_at", "")),
                modified_at=str(item.get("modified_at", "")),
                message_count=int(item.get("message_count", 0)),
                preview=str(item.get("preview", "")),
            ))
        return result

    def _write_index(self, index: list[SessionInfo]) -> None:
        data = [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "modified_at": s.modified_at,
                "message_count": s.message_count,
                "preview": s.preview,
            }
            for s in index
        ]
        self._index_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_index(self, info: SessionInfo) -> None:
        index = self._load_index()
        index.append(info)
        self._write_index(index)

    def _update_index(
        self,
        session_id: str,
        title: str = "",
        message_count: int = 0,
        preview: str = "",
        only_overwrite_placeholder: bool = False,
    ) -> None:
        index = self._load_index()
        for s in index:
            if s.id == session_id:
                s.modified_at = _now_iso()
                if title:
                    if not only_overwrite_placeholder or s.title.startswith("New session"):
                        s.title = title
                s.message_count = message_count
                s.preview = preview
                self._write_index(index)
                return

    def _write_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        path = self._snapshot_path(session_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
