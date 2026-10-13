"""Session catalog and lifecycle use cases; no FastAPI dependency.

SessionController owns only *live* sessions and worker threads.
SessionManager owns disk metadata and snapshots.
This service coordinates both without mutating SessionController internals.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from core.runtime.session_controller import SessionController
from core.session_manager import SessionManager
from .errors import InvalidInputError, NotFoundError

logger = logging.getLogger(__name__)
_SESSION_ID = re.compile(r"[0-9a-f]{12}\Z")


class SessionService:
    def __init__(
        self,
        controller: SessionController,
        agent_factory: Callable[[], Any],
        data_dir: str | Path,
        catalog: SessionManager | None = None,
    ):
        self.controller = controller
        self.agent_factory = agent_factory
        self.data_dir = Path(data_dir)
        self.catalog = catalog if catalog is not None else SessionManager()
        self._pinned: set[str] = set()

    @staticmethod
    def _valid_id(session_id: str) -> bool:
        return bool(_SESSION_ID.fullmatch(session_id))

    def _require_known(self, session_id: str):
        if not self._valid_id(session_id):
            raise NotFoundError("Session not found")
        session = self.controller.get_session(session_id)
        if session is None and self.catalog.get_session(session_id) is None:
            raise NotFoundError("Session not found")
        return session

    def create(self, title: str = "") -> dict:
        sid = self.controller.create_session(self.agent_factory)
        session = self.controller.get_session(sid)
        display = title or "新会话"
        if session is not None:
            session.title = display
        if title:
            self.catalog.rename_session(sid, title)
        return {"id": sid, "title": display, "busy": False}

    def list(self) -> list[dict]:
        ids = set(self.controller.list_sessions())
        result: list[dict] = []
        for sid in ids:
            session = self.controller.get_session(sid)
            result.append({
                "id": sid, "title": session.title or sid[:8] if session else sid[:8],
                "busy": bool(session.busy) if session else False,
                "pinned": sid in self._pinned,
            })
        try:
            for info in self.catalog.list_sessions():
                if info.id not in ids:
                    result.append({
                        "id": info.id, "title": info.title or info.id[:8],
                        "busy": False, "pinned": info.id in self._pinned,
                    })
        except (OSError, ValueError) as exc:
            logger.warning("Unable to list persisted sessions: %s", type(exc).__name__)
        result.sort(key=lambda item: (not item["pinned"], item["id"]))
        return result

    def get(self, sid: str) -> dict:
        session = self.controller.get_session(sid) if self._valid_id(sid) else None
        if session is None:
            raise NotFoundError("Session not found")
        return {
            "id": sid, "title": session.title or sid[:8],
            "busy": session.busy, "pinned": sid in self._pinned,
        }

    def activate(self, sid: str) -> dict:
        if not self._valid_id(sid):
            raise NotFoundError("Session not found on disk")
        if self.controller.get_session(sid) is not None:
            return {"status": "active"}
        info = self.catalog.get_session(sid)
        if info is None or self.catalog.load_session(sid) is None:
            raise NotFoundError("Session not found on disk")
        self.controller.attach_session(sid, self.agent_factory, title=info.title)
        return {"status": "activated", "id": sid}

    def delete(self, sid: str) -> dict:
        self._require_known(sid)
        mem_deleted = self.controller.delete_session(sid)
        disk_deleted = self.catalog.delete_session(sid)
        if not mem_deleted and not disk_deleted:
            raise NotFoundError("Session not found")
        # ID is validated before filesystem access; never resolve arbitrary paths.
        workspace = self.data_dir / "sessions" / sid
        if workspace.exists():
            shutil.rmtree(workspace)
        (self.catalog._dir / f"{sid}.turn.json").unlink(missing_ok=True)
        self._pinned.discard(sid)
        return {"status": "deleted"}

    def rename(self, sid: str, title: str) -> dict:
        session = self.get(sid)
        new_title = title.strip()
        if not new_title:
            raise InvalidInputError("Title is required")
        if not self.catalog.rename_session(sid, new_title):
            raise NotFoundError("Session not found")
        live = self.controller.get_session(sid)
        if live is not None:
            live.title = new_title
        return {"status": "renamed", "title": new_title}

    def toggle_pin(self, sid: str) -> dict:
        self.get(sid)
        if sid in self._pinned:
            self._pinned.remove(sid)
            return {"pinned": False}
        self._pinned.add(sid)
        return {"pinned": True}

    def history(self, sid: str) -> dict:
        # Historical API returns an empty list for absent snapshots.
        if not self._valid_id(sid) or self.catalog.get_session(sid) is None:
            return {"messages": []}
        snap = self.catalog.load_session(sid)
        if not isinstance(snap, dict):
            return {"messages": []}
        entries = snap.get("history_entries") or snap.get("history_messages") or []
        messages: list[dict[str, str]] = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                role = entry.get("role")
                content = entry.get("content")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": str(content)[:5000]})
        return {"messages": messages}
