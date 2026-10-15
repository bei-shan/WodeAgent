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
import time
import uuid
from typing import Any, Callable, Optional

from core.events import AgentEvent, EventSink
from core.runtime.execution_history import ExecutionHistory
from core.runtime.event_journal import EventJournal
from core.runtime.turn_state import TurnOutcome, TurnStatus, failure_outcome, persist_turn_outcome, load_turn_outcome

logger = logging.getLogger(__name__)


# ── Event types added by the session layer ───────────────────────────
# (These are NOT emitted by CodeAgent — they're session-management events.)

SESSION_EVENT_PERMISSION = "permission.requested"
SESSION_EVENT_ASK_USER = "ask_user.requested"
SESSION_EVENT_TURN_DONE = "turn.completed"
SESSION_EVENT_TURN_FINISHED = "turn.finished"
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
        self._agent_init_lock = threading.Lock()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._closed = False
        # Turn-scoped signal for interactive waiters. The Agent's own
        # interruption event is consulted separately by tool/LLM runtimes.
        self._interaction_cancelled = threading.Event()
        self.last_turn = load_turn_outcome(session_id)

        # Bounded, replayable journal; each WebSocket has an independent cursor.
        history = ExecutionHistory(workspace_base)
        self.events = EventJournal(on_append=lambda event: history.append(self.session_id, event))

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
            if self._busy or self._closed:
                return False
            self._interaction_cancelled.clear()
            self._busy = True

        # Auto-name session from first user message.
        if not self.title or self.title == "新会话":
            self.title = content.strip()[:40]

        try:
            self.ensure_agent()
        except Exception:
            with self._busy_lock:
                self._busy = False
            raise
        # An interrupt may have arrived while the lazy Agent was initializing.
        # Propagate it before the worker starts, rather than losing the request.
        if self._interaction_cancelled.is_set() and hasattr(self._agent, "interrupt"):
            self._agent.interrupt()
        self._thread = threading.Thread(
            target=self._run_turn,
            args=(content,),
            daemon=True,
            name=f"agent-{self.session_id[:8]}",
        )
        try:
            self._thread.start()
        except Exception:
            with self._busy_lock:
                self._busy = False
            raise
        return True

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        """Unblock a pending permission request.

        Returns ``True`` if the request was found and resolved,
        ``False`` if it already timed out or doesn't exist.
        """
        with self._perm_lock:
            if self._interaction_cancelled.is_set():
                return False
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
            if self._interaction_cancelled.is_set():
                return False
            entry = self._pending_ask_user.pop(request_id, None)
            self._pending_ask_events.pop(request_id, None)
        if entry is None:
            return False
        event, result = entry
        result["answer"] = answer
        event.set()
        return True

    def interrupt(self) -> bool:
        """Stop the turn and wake permission/AskUser waiters immediately.

        This does not forcibly interrupt a third-party provider request, but
        no UI approval can be granted after the cancellation was requested.
        """
        with self._busy_lock:
            if not self._busy:
                return False
            self._interaction_cancelled.set()
            agent = self._agent
        try:
            if agent is not None and hasattr(agent, "interrupt"):
                agent.interrupt()
        finally:
            # A buggy third-party interrupt hook must not strand approval
            # prompts for their full timeout or permit late authorization.
            self._release_interaction_waiters()
        return True

    def _release_interaction_waiters(self) -> None:
        """Resolve all pending prompts with safe cancellation defaults."""
        with self._perm_lock:
            for event, result in self._pending_permissions.values():
                result["decision"] = "denied"
                event.set()
            self._pending_permissions.clear()
            self._pending_permission_events.clear()
        with self._ask_lock:
            for event, result in self._pending_ask_user.values():
                result["answer"] = ""
                event.set()
            self._pending_ask_user.clear()
            self._pending_ask_events.clear()

    @property
    def busy(self) -> bool:
        return self._busy

    def ensure_agent(self) -> Any:
        """Return the lazy agent; initialize it exactly once across API/worker calls."""
        with self._agent_init_lock:
            self._ensure_agent()
            return self._agent

    def session_tree(self) -> dict:
        """Return the current tree from an idle session (no concurrent writes)."""
        with self._busy_lock:
            if self._busy:
                raise RuntimeError("Session is running")
            history = self.ensure_agent().history_manager
            return history.get_tree()

    def fork_history(self, target_id: str) -> dict:
        """Atomically switch the selected branch and persist before unlocking."""
        with self._busy_lock:
            if self._busy:
                raise RuntimeError("Session is running")
            agent = self.ensure_agent()
            history = agent.history_manager
            previous_entries = history.serialize_entries()
            previous_cursor = history.get_cursor_id()
            previous_labels = dict(history._labels)
            # A fork never deletes the abandoned branch: it moves the cursor.
            history.fork(target_id)
            try:
                snapshot = agent._build_snapshot()
                agent._session_manager.save_session(self.session_id, snapshot)
            except Exception:
                # A save error must not leave an unpersisted branch in memory.
                history.load_entries(previous_entries)
                history.restore_tree_state(previous_cursor, previous_labels)
                raise
            return {"cursor_id": history.get_cursor_id()}

    @property
    def agent(self) -> Any:
        """The active agent if already initialized; does not cause side effects."""
        return self._agent

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
                if hasattr(agent.history_manager, "restore_tree_state"):
                    agent.history_manager.restore_tree_state(
                        snapshot.get("cursor_id"), snapshot.get("labels"),
                    )
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
        """Execute exactly one turn; publish one canonical terminal outcome."""
        try:
            response = self._agent.run(content)
            status = getattr(self._agent, "last_turn_status", "completed")
            if status not in {"completed", "failed", "cancelled"}:
                status = "completed"
            outcome = TurnOutcome(
                TurnStatus(status),
                response=response,
                error_kind=getattr(self._agent, "last_turn_error_kind", None)
                if status == "failed" else None,
            )
        except Exception as exc:
            logger.exception("Agent turn failed in session %s", self.session_id[:8])
            outcome = failure_outcome(exc)
        self.last_turn = outcome
        try:
            persist_turn_outcome(self.session_id, outcome)
        except Exception:
            logger.exception("Unable to persist turn outcome for %s", self.session_id[:8])
        # Publish both terminal events before the session becomes idle.
        # A polling client seeing busy=False can then safely assume the
        # terminal journal entries already exist. Even if publication fails,
        # release the busy flag so the session cannot become stuck forever.
        try:
            self.events.put(AgentEvent(SESSION_EVENT_TURN_FINISHED, outcome.to_dict()))
            # Legacy consumers still recognize turn.completed; carry the
            # same canonical status as turn.finished.
            self.events.put(AgentEvent(SESSION_EVENT_TURN_DONE, outcome.to_dict()))
        finally:
            with self._busy_lock:
                self._busy = False

    def close(self) -> None:
        """Release idle resources and wake outstanding interactive waiters."""
        self._interaction_cancelled.set()
        self._release_interaction_waiters()
        if self._agent is not None and hasattr(self._agent, "close"):
            try:
                self._agent.close()
            except Exception:
                logger.exception("Error closing agent for session %s", self.session_id[:8])


# ═══════════════════════════════════════════════════════════════════════
# SessionController — create / list / delete sessions
# ═══════════════════════════════════════════════════════════════════════

class SessionController:
    """Manage multiple AgentSession instances."""

    def __init__(self, workspace_base: str = ".mycodeagent/sessions"):
        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()
        self._workspace_base = workspace_base
        self._closing = False

    def create_session(self, agent_factory: Callable[[], Any]) -> str:
        """Create a new session.  Returns the session ID."""
        # Reserve the same ID in the persistent catalog before first turn.
        from core.session_manager import SessionManager
        with self._lock:
            if self._closing:
                raise RuntimeError("Session controller is shutting down")
            session_id = SessionManager().create_session()
            session = AgentSession(session_id, agent_factory, workspace_base=self._workspace_base)
            self._sessions[session_id] = session
        logger.info("Session created: %s", session_id)
        return session_id

    def attach_session(
        self, session_id: str, agent_factory: Callable[[], Any], title: str = "",
    ) -> AgentSession:
        """Attach an existing persisted session without creating a throwaway ID.

        This is atomic and idempotent for concurrent reconnecting clients.
        The application service validates persistence before calling this.
        """
        if len(session_id) != 12 or any(c not in "0123456789abcdef" for c in session_id):
            raise ValueError("Invalid session ID")
        with self._lock:
            if self._closing:
                raise RuntimeError("Session controller is shutting down")
            session = self._sessions.get(session_id)
            if session is None:
                session = AgentSession(
                    session_id, agent_factory, workspace_base=self._workspace_base,
                )
                session.title = title
                self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete an idle session only; never close a running Agent.

        Mark the session closed under the same lock as send_message so a
        previously acquired session reference cannot restart a deleted turn.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            with session._busy_lock:
                if session._busy:
                    return False
                session._closed = True
                del self._sessions[session_id]
        session.close()
        logger.info("Session deleted: %s", session_id)
        return True

    def list_sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def shutdown(self, grace_seconds: float = 2.0) -> dict[str, int]:
        """Stop admitting turns, request cancellation and drain workers.

        Closing an Agent while its thread still uses the MCP client, worktree
        or trace writer is unsafe. Active sessions that ignore cancellation
        are *not* closed. They retain their in-memory controller reference and
        durable on-disk state until the process terminates.
        """
        if not 0 <= grace_seconds <= 30:
            raise ValueError("grace_seconds must be between 0 and 30")
        with self._lock:
            self._closing = True
            sessions = tuple(self._sessions.items())

        # Quiesce admissions before requesting any cancellation.
        for _sid, session in sessions:
            with session._busy_lock:
                session._closed = True

        for sid, session in sessions:
            if session.busy:
                try:
                    session.interrupt()
                except Exception:
                    logger.warning("Unable to request shutdown of session %s", sid[:8])

        deadline = time.monotonic() + grace_seconds
        closed = 0
        pending = 0
        for sid, session in sessions:
            thread = session._thread
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if self.delete_session(sid):
                closed += 1
            else:
                pending += 1
                logger.warning(
                    "Session %s still running at shutdown deadline; Agent resources left open",
                    sid[:8],
                )
        return {"closed": closed, "running": pending}


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
            if self._session._interaction_cancelled.is_set():
                return "denied"
            self._session._pending_permissions[request_id] = (done, result)
            self._session._pending_permission_events[request_id] = prompt

        self._session.events.put(prompt)

        # Block until frontend responds (or timeout).
        if not done.wait(timeout=self.PERMISSION_TIMEOUT):
            with self._session._perm_lock:
                self._session._pending_permissions.pop(request_id, None)
                self._session._pending_permission_events.pop(request_id, None)
            return "denied"

        if self._session._interaction_cancelled.is_set():
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
            if self._session._interaction_cancelled.is_set():
                return ""
            self._session._pending_ask_user[request_id] = (done, result)
            self._session._pending_ask_events[request_id] = question

        self._session.events.put(question)

        if not done.wait(timeout=self.ASK_TIMEOUT):
            with self._session._ask_lock:
                self._session._pending_ask_user.pop(request_id, None)
                self._session._pending_ask_events.pop(request_id, None)
            return ""

        if self._session._interaction_cancelled.is_set():
            return ""
        return result.get("answer", "")
