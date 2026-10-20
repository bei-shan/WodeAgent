"""Turn lifecycle, permission and AskUser use cases (transport independent)."""
from __future__ import annotations

from core.runtime.session_controller import SessionController
from core.runtime.admission import TurnCapacityExceeded
from core.runtime.turn_state import load_turn_outcome
from core.session_manager import SessionManager
from .errors import CapacityError, ConflictError, NotFoundError


class TurnService:
    def __init__(self, controller: SessionController, catalog: SessionManager | None = None):
        self.controller = controller
        self.catalog = catalog if catalog is not None else SessionManager()

    def _require_active(self, sid: str):
        session = self.controller.get_session(sid)
        if session is None:
            raise NotFoundError("Session not found")
        return session

    def status(self, sid: str) -> dict:
        session = self.controller.get_session(sid)
        if session is None and self.catalog.get_session(sid) is None:
            raise NotFoundError("Session not found")
        if session is not None and session.busy:
            return {"status": "running"}
        outcome = session.last_turn if session is not None else load_turn_outcome(sid)
        return outcome.to_dict() if outcome is not None else {"status": "idle"}

    def send(self, sid: str, content: str, attachments: list[dict] | None = None) -> dict:
        session = self._require_active(sid)
        parts = attachments or []
        if parts:
            # Only names enter durable history; media blocks remain memory-
            # only until the first model invocation in this turn.
            import json
            refs = [p["path"] for p in parts]
            content = (content.strip() or "Analyze the attached files.") + (
                "\n[Session attachments: " + json.dumps(refs, ensure_ascii=False) + "]"
            )
        try:
            accepted = session.send_message(content, attachment_parts=parts)
        except TurnCapacityExceeded as exc:
            raise CapacityError("Agent turn capacity exhausted; retry later") from exc
        if not accepted:
            raise ConflictError("Agent is busy — wait for turn.finished event")
        return {"status": "accepted"}

    def interrupt(self, sid: str) -> dict:
        if not self._require_active(sid).interrupt():
            raise ConflictError("Agent is not running or does not support cancellation")
        return {"status": "interrupted"}

    def resolve_permission(self, sid: str, request_id: str, decision: str) -> dict:
        if not self._require_active(sid).resolve_permission(request_id, decision):
            raise NotFoundError("Permission request not found or already timed out")
        return {"status": "resolved", "decision": decision}

    def answer_ask_user(self, sid: str, request_id: str, answer: str) -> dict:
        if not self._require_active(sid).answer_ask_user(request_id, answer):
            raise NotFoundError("AskUser request not found or timed out")
        return {"status": "answered"}
