"""Session controller — run agent turns in worker threads, relay events.

Why this exists
==============
``CodeAgent.run()`` is synchronous and blocking (LLM calls, tool execution,
MCP — all synchronous).  You cannot call it directly from a FastAPI async
handler or a WebSocket event loop without freezing the server.

This module wraps each agent session in a dedicated **worker thread** and
relays all agent lifecycle events through a **thread-safe queue**.  UI layers
(CLI, Web, desktop) read from the queue instead of calling agent methods
directly.

Permission / AskUser wait
-------------------------
When the agent needs user input (permission dialog, AskUser tool), the
worker thread **blocks** on a ``threading.Event``.  The frontend receives
a corresponding event, collects the user's answer, and calls
``resolve_permission()`` or ``answer_ask_user()`` to unblock the worker.

Key guarantees
--------------
- One turn at a time per session (``send_message`` returns False if busy).
- Thread-safe: event journal, events, and pending-dict are all guarded.
- WebSocket subscribers replay by sequence without stealing each other's events.
- No dependency on any web framework — pure stdlib.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Callable, Optional

from core.events import AgentEvent, EventSink
from core.runtime.event_journal import EventJournal

logger = logging.getLogger(__name__)


# ── Event types added by the session layer ───────────────────────────
# (These are NOT emitted by CodeAgent — they're session-management events.)

SESSION_EVENT_PERMISSION = "permission.requested"
SESSION_EVENT_ASK_USER = "ask_user.requested"
SESSION_EVENT_TURN_DONE = "turn.completed"
SESSION_EVENT_ERROR = "error"


# ═══════════════════════════════════════════════════════════════════════
# AgentSession — one agent, one thread, one event queue
# ═══════════════════════════════════════════════════════════════════════

class AgentSession:
    """A single agent session with its own worker thread and event queue.

    Create via ``SessionController.create_session()`` — do not instantiate
    directly.
    """

    def __init__(
        self,
        session_id: str,
        agent_factory: Callable[[], Any],  # () -> CodeAgent
        workspace_base: str = ".mycodeagent/sessions",
    ):
        self.session_id = session_id
        self.title: str = ""  # auto-set from first user message
        self.workspace_dir: str = ""  # set in _ensure_agent, used by file download
        self._agent_factory = agent_factory
        self._workspace_base = workspace_base
        self._agent: Any = None  # created lazily on first send_message
        self._thread: Optional[threading.Thread] = None
        self._busy_lock = threading.Lock()
        self._busy = False

        # Bounded, replayable journal; each WebSocket has an independent cursor.
        self.events = EventJournal()

        # Pending permission / ask-user requests.
        #   {request_id: (threading.Event, dict-for-result)}
        self._pending_permissions: dict[str, tuple[threading.Event, dict]] = {}
        self._pending_ask_user: dict[str, tuple[threading.Event, dict]] = {}
        self._perm_lock = threading.Lock()
        self._ask_lock = threading.Lock()
        self._pending_permission_events: dict[str, AgentEvent] = {}
        self._pending_ask_events: dict[str, AgentEvent] = {}

    def pending_prompts(self) -> list[AgentEvent]:
        """Snapshot prompts still waiting for UI input (for reconnects)."""
        with self._perm_lock:
            permissions = list(self._pending_permission_events.values())
        with self._ask_lock:
            asks = list(self._pending_ask_events.values())
        return permissions + asks

    # ── Public API ────────────────────────────────────────────────────

    def send_message(self, content: str) -> bool:
        """Kick off an agent turn in the worker thread.

        Auto-names the session from the first user message.
        Returns ``True`` if the turn started, ``False`` if the agent is
        already busy (the caller should wait for ``turn.completed``).
        """
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True

        # Auto-name session from first user message.
        if not self.title or self.title == "新会话":
            self.title = content.strip()[:40]

        try:
            self._ensure_agent()
        except Exception:
            with self._busy_lock:
                self._busy = False
            raise
        self._thread = threading.Thread(
            target=self._run_turn,
            args=(content,),
            daemon=True,
            name=f"agent-{self.session_id[:8]}",
        )
        self._thread.start()
        return True

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        """Unblock a pending permission request.

        Returns ``True`` if the request was found and resolved,
        ``False`` if it already timed out or doesn't exist.
        """
        with self._perm_lock:
            entry = self._pending_permissions.pop(request_id, None)
            self._pending_permission_events.pop(request_id, None)
        if entry is None:
            return False
        event, result = entry
        result["decision"] = decision
        event.set()
        return True

    def answer_ask_user(self, request_id: str, answer: str) -> bool:
        """Unblock a pending AskUser request."""
        with self._ask_lock:
            entry = self._pending_ask_user.pop(request_id, None)
            self._pending_ask_events.pop(request_id, None)
        if entry is None:
            return False
        event, result = entry
        result["answer"] = answer
        event.set()
        return True

    def interrupt(self) -> bool:
        """Request the agent to stop.  Best-effort (not preemptive)."""
        if self._agent is not None and hasattr(self._agent, "interrupt"):
            self._agent.interrupt()
            return True
        return False

    @property
    def busy(self) -> bool:
        return self._busy

    # ── Internal ──────────────────────────────────────────────────────

    def _ensure_agent(self) -> None:
        """Lazy-create the CodeAgent on first use.

        Creates a session-specific workspace under .mycodeagent/sessions/<sid>/
        so files generated by the agent are isolated from other sessions.
        """
        if self._agent is not None:
            return

        import os
        from pathlib import Path

        # Session-specific workspace
        workspace = Path(self._workspace_base) / self.session_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = str(workspace.resolve())

        agent = self._agent_factory()
        # A CodeAgent initializes a temporary session ID in SessionFeature.
        # Reuse the Web ID instead so history, snapshots and workspace agree.
        store = getattr(agent, "_session_manager", None)
        old_id = getattr(agent, "_session_id", None)
        if store is not None and old_id != self.session_id:
            store.delete_session(old_id)
            agent._session_id = self.session_id
            snapshot = store.load_session(self.session_id) or {}
            if snapshot.get("history_entries"):
                agent.history_manager.load_entries(snapshot["history_entries"])
            elif snapshot.get("history_messages"):
                agent.history_manager.load_messages(snapshot["history_messages"])

        # Redirect agent to session workspace using the built-in rebind
        # which properly updates project_root, permission gate, tools,
        # context builder, skill loader, and team manager.
        agent._rebind_project_root(str(workspace))
        agent._original_project_root = str(workspace)

        # Install session-scoped event sink → queue.
        agent.event_sink = _QueueEventSink(self.events)
        # Stream content tokens to the Web event queue, including reasoning
        # metadata (the UI deliberately displays only content deltas).
        agent.llm_stream_callback = lambda kind, text, step: self.events.put(
            AgentEvent("llm.delta", {"kind": kind, "text": text}, step=step)
        )

        # Wire permission broker → blocks on permission.requested events.
        broker = _SessionPermissionBroker(self)
        if hasattr(agent, "_permission_gate") and agent._permission_gate:
            agent._permission_gate._broker = broker

        # Wire AskUser input function → blocks on ask_user.requested events.
        try:
            ask_tool = agent.tool_registry.get_tool("AskUser")
            if ask_tool is not None:
                ask_tool._input_func = _SessionAskUserFunc(self)
        except Exception:
            pass

        self._agent = agent
        logger.info("Agent created for session %s", self.session_id[:8])

    def _run_turn(self, content: str) -> None:
        """Execute one agent turn (runs in worker thread)."""
        try:
            response = self._agent.run(content)
            self.events.put(AgentEvent(SESSION_EVENT_TURN_DONE, {
                "response": response,
            }))
        except Exception as exc:
            logger.exception("Agent turn failed in session %s", self.session_id[:8])
            self.events.put(AgentEvent(SESSION_EVENT_ERROR, {
                "message": str(exc),
            }))
        finally:
            with self._busy_lock:
                self._busy = False

    def close(self) -> None:
        """Release agent resources (MCP clients, worktree, trace logger)."""
        if self._agent is not None and hasattr(self._agent, "close"):
            try:
                self._agent.close()
            except Exception:
                logger.exception("Error closing agent for session %s", self.session_id[:8])
        # Unblock any remaining waiters.
        with self._perm_lock:
            for event, result in self._pending_permissions.values():
                result.setdefault("decision", "denied")
                event.set()
            self._pending_permissions.clear()
            self._pending_permission_events.clear()
        with self._ask_lock:
            for event, result in self._pending_ask_user.values():
                result.setdefault("answer", "")
                event.set()
            self._pending_ask_user.clear()
            self._pending_ask_events.clear()


# ═══════════════════════════════════════════════════════════════════════
# SessionController — create / list / delete sessions
# ═══════════════════════════════════════════════════════════════════════

class SessionController:
    """Manage multiple AgentSession instances."""

    def __init__(self, workspace_base: str = ".mycodeagent/sessions"):
        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()
        self._workspace_base = workspace_base

    def create_session(self, agent_factory: Callable[[], Any]) -> str:
        """Create a new session.  Returns the session ID."""
        # Reserve the same ID in the persistent catalog before first turn.
        from core.session_manager import SessionManager
        session_id = SessionManager().create_session()
        session = AgentSession(session_id, agent_factory, workspace_base=self._workspace_base)
        with self._lock:
            self._sessions[session_id] = session
        logger.info("Session created: %s", session_id)
        return session_id

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        logger.info("Session deleted: %s", session_id)
        return True

    def list_sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers (not public API)
# ═══════════════════════════════════════════════════════════════════════

class _QueueEventSink(EventSink):
    """EventSink that pushes agent events into a thread-safe queue."""

    def __init__(self, q: EventJournal):
        self._queue = q

    def emit(self, event: AgentEvent) -> None:
        self._queue.put(event)


class _SessionPermissionBroker:
    """PermissionGate._broker — blocks the agent thread until the
    frontend calls session.resolve_permission()."""

    PERMISSION_TIMEOUT = 120  # seconds

    def __init__(self, session: AgentSession):
        self._session = session

    def __call__(self, resolved_path: str, tool_name: str, action: str) -> str:
        request_id = uuid.uuid4().hex[:8]
        done = threading.Event()
        result: dict[str, str] = {}

        prompt = AgentEvent(SESSION_EVENT_PERMISSION, {
            "request_id": request_id,
            "tool": tool_name,
            "path": resolved_path,
            "action": action,
        })
        with self._session._perm_lock:
            self._session._pending_permissions[request_id] = (done, result)
            self._session._pending_permission_events[request_id] = prompt

        self._session.events.put(prompt)

        # Block until frontend responds (or timeout).
        if not done.wait(timeout=self.PERMISSION_TIMEOUT):
            with self._session._perm_lock:
                self._session._pending_permissions.pop(request_id, None)
                self._session._pending_permission_events.pop(request_id, None)
            return "denied"

        return result.get("decision", "denied")


class _SessionAskUserFunc:
    """AskUserTool._input_func — blocks the agent thread until the
    frontend calls session.answer_ask_user()."""

    ASK_TIMEOUT = 300  # seconds — users need time to read & answer

    def __init__(self, session: AgentSession):
        self._session = session

    def __call__(self, prompt: str) -> str:
        request_id = uuid.uuid4().hex[:8]
        done = threading.Event()
        result: dict[str, str] = {}

        question = AgentEvent(SESSION_EVENT_ASK_USER, {
            "request_id": request_id,
            "prompt": prompt,
        })
        with self._session._ask_lock:
            self._session._pending_ask_user[request_id] = (done, result)
            self._session._pending_ask_events[request_id] = question

        self._session.events.put(question)

        if not done.wait(timeout=self.ASK_TIMEOUT):
            with self._session._ask_lock:
                self._session._pending_ask_user.pop(request_id, None)
                self._session._pending_ask_events.pop(request_id, None)
            return ""

        return result.get("answer", "")
