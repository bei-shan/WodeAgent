"""SessionFeature — multi-conversation session management."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class SessionFeature(AgentFeature):
    """Manages multiple conversation sessions with auto-save and resume.

    Sessions are stored as JSON snapshots under ``memory/sessions/``
    with an ``index.json`` tracking all sessions.
    """

    name = "session"
    order = 100

    def init(self, agent: "CodeAgent") -> None:
        from core.session_manager import SessionManager

        sessions_dir = os.path.join(agent._original_project_root, "memory", "sessions")
        agent._session_manager = SessionManager(sessions_dir=sessions_dir)
        agent._session_id = agent._session_manager.create_session()

    def cleanup(self, agent: "CodeAgent") -> None:
        """Final auto-save on shutdown."""
        try:
            snapshot = agent._build_snapshot()
            agent._session_manager.save_session(agent._session_id, snapshot)
        except Exception:
            pass

    def on_model_changed(
        self, agent: "CodeAgent", old_model: str, new_model: str
    ) -> None:
        """Record the model change to the session trace if a trace logger
        is wired up. Demonstrates the event hook without duplicating
        history_manager.append_model_change (which CodeAgent already
        called before dispatching this event)."""
        trace = getattr(agent, "trace_logger", None)
        if trace is None:
            return
        try:
            trace.log_event(
                "model_change",
                {"old_model": old_model, "new_model": new_model},
            )
        except Exception:
            pass
