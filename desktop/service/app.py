"""MyCodeAgent Web Service — FastAPI application.

Usage::

    from desktop.service.app import create_app
    app = create_app(agent_factory, tool_registry, project_root)
    # uvicorn desktop.service.app:app --host 127.0.0.1 --port 8000

Or run directly::

    python -m desktop.service.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from desktop.service.local_auth import (
    LocalWebAuth, LocalAuthMiddleware, allow_websocket,
    COOKIE_NAME, COOKIE_TTL,
)

# Ensure project root on path.
_proj = Path(__file__).resolve().parents[2]
if str(_proj) not in sys.path:
    sys.path.insert(0, str(_proj))

from core.runtime.session_controller import (
    SessionController,
    SESSION_EVENT_PERMISSION,
    SESSION_EVENT_ASK_USER,
    SESSION_EVENT_TURN_DONE,
    SESSION_EVENT_TURN_FINISHED,
    SESSION_EVENT_ERROR,
)
from core.events import AgentEvent, EventType
from desktop.service.schemas import (
    SessionCreate,
    SessionInfo,
    MessageSend,
    PermissionResolve,
    AskUserAnswer,
    FileTreeResponse,
    FileContentResponse,
    FileEntry,
    ModelInfo,
    ToolInfo,
    ToolParam,
    McpStatusResponse,
    McpServerStatus,
    SkillCreate,
    SkillUpdate,
    SkillInfo,
    McpServerConfig,
    McpServerCreate,
    McpServerUpdate,
    SessionConfig,
    SessionConfigUpdate,
    TeamCreate,
    TeamInfo,
)

logger = logging.getLogger("mycodeagent.service")

# ── Event types forwarded to frontend ─────────────────────────────────
_STREAM_EVENTS = {
    EventType.RUN_STARTED,
    EventType.RUN_FINISHED,
    EventType.STEP_STARTED,
    EventType.LLM_STARTED,
    EventType.LLM_COMPLETED,
    EventType.LLM_DELTA,
    EventType.TOOL_STARTED,
    EventType.TOOL_COMPLETED,
    EventType.ASSISTANT_FINAL,
    SESSION_EVENT_PERMISSION,
    SESSION_EVENT_ASK_USER,
    SESSION_EVENT_TURN_DONE,
    SESSION_EVENT_TURN_FINISHED,
    SESSION_EVENT_ERROR,
    # Subagent streaming events
    EventType.SUBAGENT_STARTED,
    EventType.SUBAGENT_DELTA,
    EventType.SUBAGENT_TOOL_USE,
    EventType.SUBAGENT_FINISHED,
}


# ═══════════════════════════════════════════════════════════════════════
# App factory
# ═══════════════════════════════════════════════════════════════════════

def create_app(
    agent_factory: Optional[Callable[[], Any]] = None,
    tool_registry: Any = None,
    project_root: str = ".",
) -> FastAPI:
    """Build the FastAPI application.

    Parameters
    ----------
    agent_factory:
        Zero-arg callable that returns a fresh ``CodeAgent`` instance.
        Called once per session (lazily on first ``send_message``).
    tool_registry:
        Shared ``ToolRegistry`` for tool listing.  Typically the same
        one used inside *agent_factory*.
    project_root:
        Base project directory for file browsing (before worktree).
    """

    if agent_factory is None:
        # Allows the documented uvicorn --factory entry point to work.
        from core.env import load_env
        from core.config import Config
        from core.llm import HelloAgentsLLM
        from agents.codeAgent import CodeAgent
        from tools.registry import ToolRegistry
        load_env()
        config = Config.from_env()
        def agent_factory() -> Any:
            return CodeAgent(
                name="web", llm=HelloAgentsLLM(),
                tool_registry=ToolRegistry(), project_root=project_root,
                config=config,
            )

    app = FastAPI(
        title="MyCodeAgent API",
        version="0.1.0",
        docs_url="/docs",
    )

    # CORS — permissive for local dev; tighten for production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8000", "http://127.0.0.1:8000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Read the private .env even when a custom agent factory was injected.
    # Environment variables always take precedence over dotenv defaults.
    from core.env import load_env
    load_env()

    # Optional local-only login protection. Defaults off for compatibility;
    # only a securely configured, high-entropy operator token enables it.
    configured_auth_token = os.getenv("WEB_AUTH_TOKEN", "").strip()
    auth = LocalWebAuth(configured_auth_token) if configured_auth_token else None
    if auth is not None:
        app.add_middleware(LocalAuthMiddleware, auth=auth)
    app.state.local_auth = auth

    # ── Shared state ──────────────────────────────────────────────
    import os as _os
    app.state.data_dir = _os.path.abspath(
        _os.getenv("MYCODEAGENT_DATA_DIR", _os.path.join(project_root, ".mycodeagent"))
    )
    app.state.controller = SessionController(
        workspace_base=os.path.join(app.state.data_dir, "sessions")
    )
    app.state.agent_factory = agent_factory
    app.state.tool_registry = tool_registry
    app.state.project_root = os.path.abspath(project_root)
    app.state._started_at = time.time()

    # ── Lifespan ──────────────────────────────────────────────────
    @app.on_event("startup")
    async def _startup():
        logger.info("Service started — project_root=%s", app.state.project_root)

    @app.on_event("shutdown")
    async def _shutdown():
        ctrl: SessionController = app.state.controller
        for sid in ctrl.list_sessions():
            ctrl.delete_session(sid)
        logger.info("Service shut down")

    # ═══════════════════════════════════════════════════════════════
    # Single-user local authentication
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/auth/status")
    async def local_auth_status(request: Request):
        current = app.state.local_auth
        return {
            "required": current is not None,
            "authenticated": current is None or current.authenticated(
                request.headers, request.cookies
            ),
        }

    @app.post("/api/auth/login")
    async def local_auth_login(request: Request, body: dict):
        current = app.state.local_auth
        if current is None:
            return {"status": "not_required"}
        if not current.valid_origin(request.headers.get("origin")):
            raise HTTPException(403, "Untrusted request origin")
        address = request.client.host if request.client else "unknown"
        if not current.allow_login_attempt(address):
            raise HTTPException(429, "Too many login attempts")
        token = body.get("token")
        if not isinstance(token, str) or not current.check_token(token):
            raise HTTPException(401, "Invalid login token")
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            COOKIE_NAME, current.issue_cookie(),
            max_age=COOKIE_TTL, httponly=True, samesite="strict",
            secure=request.url.scheme == "https", path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/logout")
    async def local_auth_logout():
        response = JSONResponse({"status": "logged_out"})
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return response

    # ═══════════════════════════════════════════════════════════════
    # Health
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/health")
    async def health():
        ctrl: SessionController = app.state.controller
        return {
            "status": "ok",
            "uptime": int(time.time() - app.state._started_at),
            "sessions": len(ctrl.list_sessions()),
        }

    # ═══════════════════════════════════════════════════════════════
    # Sessions
    # ═══════════════════════════════════════════════════════════════

    @app.post("/api/sessions", response_model=SessionInfo, status_code=201)
    async def create_session(body: SessionCreate):
        """Create a new agent session."""
        ctrl: SessionController = app.state.controller
        sid = ctrl.create_session(app.state.agent_factory)
        session = ctrl.get_session(sid)
        title = body.title or "新会话"
        if session:
            session.title = title
        return SessionInfo(id=sid, title=title, busy=False)

    @app.get("/api/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        """List sessions — active (in-memory) + persisted (on-disk)."""
        ctrl: SessionController = app.state.controller
        pinned = getattr(app.state, '_pinned', set())
        active_ids = set(ctrl.list_sessions())
        result = []

        # Active sessions first
        for sid in active_ids:
            session = ctrl.get_session(sid)
            title = session.title if session and session.title else sid[:8]
            result.append(SessionInfo(id=sid, title=title,
                busy=(session.busy if session else False),
                pinned=(sid in pinned)))

        # Add persisted sessions not currently active (from disk)
        try:
            from core.session_manager import SessionManager
            sm = SessionManager()
            for s in sm.list_sessions():
                if s.id not in active_ids:
                    result.append(SessionInfo(id=s.id,
                        title=s.title or s.id[:8],
                        busy=False, pinned=(s.id in pinned)))
        except Exception:
            pass

        result.sort(key=lambda s: (not s.pinned, s.id))
        return result

    @app.get("/api/sessions/{sid}", response_model=SessionInfo)
    async def get_session(sid: str):
        """Get session info."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        title = session.title or sid[:8]
        return SessionInfo(id=sid, title=title, busy=session.busy)

    @app.post("/api/sessions/{sid}/activate")
    async def activate_session(sid: str):
        """Activate a persisted session — load into memory for WebSocket use."""
        ctrl: SessionController = app.state.controller
        # Already active
        if ctrl.get_session(sid) is not None:
            return {"status": "active"}
        # Check disk
        snap_path = Path("memory/sessions") / f"{sid}.json"
        if not snap_path.exists():
            raise HTTPException(404, "Session not found on disk")
        # Create new AgentSession
        new_sid = ctrl.create_session(app.state.agent_factory)
        session = ctrl.get_session(new_sid)
        if session is None:
            raise HTTPException(500, "Failed to create session")
        # Override its ID to match the persisted one
        session.session_id = sid
        from core.session_manager import SessionManager
        previous = SessionManager().get_session(sid)
        if previous is not None:
            session.title = previous.title
        ctrl._sessions.pop(new_sid, None)
        ctrl._sessions[sid] = session
        from core.session_manager import SessionManager
        SessionManager().delete_session(new_sid)
        return {"status": "activated", "id": sid}

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        """Delete a session — from memory AND from disk."""
        import shutil
        ctrl: SessionController = app.state.controller
        # Remove from in-memory controller if active
        mem_deleted = ctrl.delete_session(sid)
        # Also remove persisted snapshot + workspace
        snap_path = Path("memory/sessions") / f"{sid}.json"
        if snap_path.exists():
            snap_path.unlink()
        ws_path = Path(app.state.data_dir) / "sessions" / sid
        if ws_path.exists():
            shutil.rmtree(ws_path)
        from core.session_manager import SessionManager
        disk_deleted = SessionManager().delete_session(sid)
        # Remove the terminal-state sidecar only after a real session existed.
        if mem_deleted or disk_deleted:
            (Path("memory/sessions") / f"{sid}.turn.json").unlink(missing_ok=True)
        if not mem_deleted and not disk_deleted:
            raise HTTPException(404, "Session not found")
        return {"status": "deleted"}

    @app.put("/api/sessions/{sid}/rename")
    async def rename_session(sid: str, body: dict):
        """Rename a session."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        new_title = (body.get("title") or "").strip()
        if not new_title:
            raise HTTPException(400, "Title is required")
        session.title = new_title
        from core.session_manager import SessionManager
        SessionManager().rename_session(sid, new_title)
        return {"status": "renamed", "title": new_title}

    # In-memory pinned sessions set
    if not hasattr(app.state, "_pinned"):
        app.state._pinned: set[str] = set()

    @app.post("/api/sessions/{sid}/pin")
    async def toggle_pin_session(sid: str):
        """Toggle pin status for a session."""
        ctrl: SessionController = app.state.controller
        if ctrl.get_session(sid) is None:
            raise HTTPException(404, "Session not found")
        if sid in app.state._pinned:
            app.state._pinned.discard(sid)
            return {"pinned": False}
        else:
            app.state._pinned.add(sid)
            return {"pinned": True}

    @app.get("/api/sessions/{sid}/history")
    async def get_session_history(sid: str):
        """Load persisted session history (messages from disk snapshot)."""
        from core.session_store import load_session_snapshot
        from pathlib import Path
        snap_path = Path("memory/sessions") / f"{sid}.json"
        if not snap_path.exists():
            return {"messages": []}
        try:
            snap = load_session_snapshot(str(snap_path))
        except Exception:
            return {"messages": []}
        # Both new tree snapshots and legacy message-list snapshots.
        entries = snap.get("history_entries") or snap.get("history_messages") or []
        messages = []
        for entry in entries:
            role = entry.get("role", "")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": str(content)[:5000]})
        return {"messages": messages}

    # ═══════════════════════════════════════════════════════════════
    # Messages
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/turn")
    async def get_turn_status(sid: str):
        """Last canonical terminal result, or running/idle state."""
        from core.runtime.turn_state import load_turn_outcome
        from core.session_manager import SessionManager
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None and SessionManager().get_session(sid) is None:
            raise HTTPException(404, "Session not found")
        if session is not None and session.busy:
            return {"status": "running"}
        outcome = session.last_turn if session else load_turn_outcome(sid)
        return outcome.to_dict() if outcome is not None else {"status": "idle"}

    @app.post("/api/sessions/{sid}/messages")
    async def send_message(sid: str, body: MessageSend):
        """Send a user message — starts an agent turn in a worker thread."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        ok = session.send_message(body.content)
        if not ok:
            raise HTTPException(409, "Agent is busy — wait for turn.finished event")
        return {"status": "accepted"}

    @app.post("/api/sessions/{sid}/interrupt")
    async def interrupt(sid: str):
        """Request the agent to stop (best-effort)."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        if not session.interrupt():
            raise HTTPException(409, "Agent is not running or does not support cancellation")
        return {"status": "interrupted"}

    @app.post("/api/sessions/{sid}/upload")
    async def upload_file(sid: str, file: UploadFile = File(...)):
        """Upload a file to the session workspace. Returns the saved path."""
        import shutil
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        # Use session workspace — ensure agent is initialized first
        from pathlib import Path as Pt
        ws = Pt(app.state.data_dir) / "sessions" / sid
        ws.mkdir(parents=True, exist_ok=True)
        # Sanitize filename
        safe_name = Pt(file.filename or "upload").name
        dest = ws / safe_name
        # Avoid overwriting: add counter if exists
        if dest.exists():
            stem, ext = Pt(safe_name).stem, Pt(safe_name).suffix
            i = 1
            while dest.exists():
                dest = ws / f"{stem}_{i}{ext}"
                i += 1
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        rel_path = dest.name  # always relative to this session's workspace
        return {"path": rel_path, "name": safe_name, "size": dest.stat().st_size}

    # ═══════════════════════════════════════════════════════════════
    # Permissions
    # ═══════════════════════════════════════════════════════════════

    @app.post("/api/sessions/{sid}/permissions/{rid}/resolve")
    async def resolve_permission(sid: str, rid: str, body: PermissionResolve):
        """Resolve a pending permission request (user clicked Allow/Deny)."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        ok = session.resolve_permission(rid, body.decision)
        if not ok:
            raise HTTPException(404, "Permission request not found or already timed out")
        return {"status": "resolved", "decision": body.decision}

    # ═══════════════════════════════════════════════════════════════
    # AskUser
    # ═══════════════════════════════════════════════════════════════

    @app.post("/api/sessions/{sid}/ask-user/{rid}/answer")
    async def answer_ask_user(sid: str, rid: str, body: AskUserAnswer):
        """Answer a pending AskUser request."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        ok = session.answer_ask_user(rid, body.answer)
        if not ok:
            raise HTTPException(404, "AskUser request not found or timed out")
        return {"status": "answered"}

    # ═══════════════════════════════════════════════════════════════
    # Session config
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/config", response_model=SessionConfig)
    async def get_session_config(sid: str):
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        if session._agent is None:
            session._ensure_agent()
        agent = session._agent
        model = getattr(agent.llm, 'model', '')
        provider = getattr(agent.llm, 'provider', '')
        teams = bool(getattr(agent, 'enable_agent_teams', False))
        plan = bool(getattr(agent, '_in_plan_mode', False))
        return SessionConfig(
            model=model, provider=provider,
            enable_agent_teams=teams, plan_mode=plan,
            thinking_level=agent.history_manager.get_thinking_level(),
        )

    @app.put("/api/sessions/{sid}/config")
    async def update_session_config(sid: str, body: SessionConfigUpdate):
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        if session.busy:
            raise HTTPException(409, "Cannot change configuration during a turn")
        if session._agent is None:
            session._ensure_agent()
        agent = session._agent
        if body.model_profile is not None:
            from core.model_profiles import load_model_profiles
            profiles = load_model_profiles()
            if body.model_profile == "default" and not profiles:
                pass  # already using the configured default LLM
            elif body.model_profile not in profiles:
                raise HTTPException(400, "Unknown model profile")
            else:
                agent.switch_model(model=body.model_profile)
        if body.enable_agent_teams is not None:
            agent.enable_agent_teams = bool(body.enable_agent_teams)
            if hasattr(agent.config, 'enable_agent_teams'):
                agent.config.enable_agent_teams = agent.enable_agent_teams
            # Lazy-init team manager when toggled on after agent start.
            if agent.enable_agent_teams and agent.team_manager is None:
                try:
                    from core.team_engine.manager import TeamManager
                    from core.team_engine.display_mode import resolve_teammate_mode
                    mode, _ = resolve_teammate_mode(
                        str(getattr(agent.config, 'teammate_mode', 'auto') or 'auto')
                    )
                    agent.team_manager = TeamManager(
                        store_dir=str(getattr(agent.config, 'agent_teams_store_dir', '.teams') or '.teams'),
                        task_store_dir=str(getattr(agent.config, 'agent_tasks_store_dir', '.tasks') or '.tasks'),
                        teammate_mode=mode,
                    )
                    from core.tool_bootstrap import register_team_tools
                    register_team_tools(agent._tool_bootstrap if hasattr(agent, '_tool_bootstrap') else None)
                except Exception:
                    pass
        if body.plan_mode is not None:
            if body.plan_mode:
                agent.enter_plan_mode()
            else:
                agent.exit_plan_mode("")
        return {"status": "updated"}

    # ═══════════════════════════════════════════════════════════════
    # Agent teams
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/teams", response_model=list[TeamInfo])
    async def list_teams(sid: str):
        """List agent teams and their task board status."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None or session._agent is None:
            return []
        agent = session._agent
        if not getattr(agent, 'enable_agent_teams', False) or agent.team_manager is None:
            return []
        try:
            state = agent.team_manager.export_state()
        except Exception:
            return []
        work_items = state.get("work_items", {})
        teams_data = state.get("teams", {})
        result = []
        for team_name, team_state in teams_data.items():
            result.append(TeamInfo(
                name=team_name,
                queued=team_state.get("queued", 0),
                running=team_state.get("running", 0),
                succeeded=team_state.get("succeeded", 0),
                failed=team_state.get("failed", 0),
                active=team_state.get("active", 0),
                idle=team_state.get("idle", 0),
                approvals_pending=team_state.get("approvals_pending", 0),
                blocked=team_state.get("blocked", 0),
            ))
        return result

    @app.post("/api/sessions/{sid}/teams", status_code=201)
    async def create_team(sid: str, body: TeamCreate):
        """Create an agent team."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None or session._agent is None:
            raise HTTPException(404, "Session not found")
        agent = session._agent
        if not getattr(agent, 'enable_agent_teams', False) or agent.team_manager is None:
            raise HTTPException(400, "Agent teams not enabled. Toggle enable_agent_teams in session config first.")
        try:
            team = agent.team_manager.create_team(body.team_name)
            return {"status": "created", "team": team.get("name", body.team_name)}
        except Exception as exc:
            raise HTTPException(500, str(exc))

    # ═══════════════════════════════════════════════════════════════
    # WebSocket — event stream
    # ═══════════════════════════════════════════════════════════════

    @app.websocket("/api/sessions/{sid}/stream")
    async def event_stream(ws: WebSocket, sid: str):
        """Stream agent events to the frontend in real time."""
        if not allow_websocket(ws, app.state.local_auth):
            await ws.close(code=1008)
            return
        await ws.accept()

        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            await ws.send_json({"type": "error", "payload": {"message": "Session not found"}})
            await ws.close()
            return

        # No cursor: join live at the current watermark. Existing sessions
        # load their durable conversation from /history; active permission
        # prompts are separately restored below.
        # With ?after=N: replay N+1..latest, then follow new events without
        # racing or competing with any other connected WebSocket.
        raw_cursor = ws.query_params.get("after")
        if raw_cursor is None:
            # An active turn has not yet been snapshotted to disk. Replay its
            # in-flight events to a newly opened browser instead of silently
            # starting at the newest event and losing the partial response.
            cursor = session.events.active_turn_cursor() if session.busy else session.events.latest_seq
        else:
            try:
                cursor = int(raw_cursor)
                if cursor < 0:
                    raise ValueError("negative sequence")
            except ValueError:
                await ws.close(code=1008, reason="Invalid event cursor")
                return

        async def send_event(event: AgentEvent, seq: int | None = None) -> None:
            if event.type not in _STREAM_EVENTS:
                return
            frame = {
                "type": event.type,
                "payload": _sanitize_payload(event.payload),
                "step": event.step,
            }
            if seq is not None:
                frame["seq"] = seq
            await ws.send_json(frame)

        # A process restart resets the in-memory journal. An old browser
        # can reconnect with a cursor greater than the new journal's latest.
        # Explicitly rebase that cursor or it would wait indefinitely.
        if cursor > session.events.latest_seq:
            cursor = session.events.latest_seq
            await ws.send_json({
                "type": "stream.reset",
                "payload": {
                    "reason": "cursor_ahead",
                    "oldest_seq": cursor + 1,
                    "latest_seq": cursor,
                    "resume_from": cursor,
                },
                "step": 0,
            })

        # Permission and AskUser are blocking prompts; a browser refresh
        # must not strand a worker waiting for an invisible dialog.
        if raw_cursor is None:
            for pending in session.pending_prompts():
                await send_event(pending)

        disconnected = asyncio.create_task(ws.receive_text())
        try:
            while not disconnected.done():
                # Condition wait runs in a worker, never blocks FastAPI's loop.
                batch = await asyncio.to_thread(session.events.wait_after, cursor, 0.5, 128)
                if disconnected.done():
                    break
                if batch.gap:
                    # A slow/offline client missed the ring-buffer window.
                    # Tell it to reload durable history instead of pretending
                    # every token/event was delivered.
                    await ws.send_json({
                        "type": "stream.reset",
                        "payload": {
                            "reason": "cursor_expired",
                            "oldest_seq": batch.oldest_seq,
                            "latest_seq": batch.latest_seq,
                            "resume_from": batch.oldest_seq - 1,
                        },
                        "step": 0,
                    })
                    cursor = batch.oldest_seq - 1
                for item in batch.events:
                    await send_event(item.event, item.seq)
                    cursor = item.seq

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WebSocket error for session %s", sid[:8])
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            # Agent runs and journal entries outlive individual sockets.

    # ═══════════════════════════════════════════════════════════════
    # Files — session-scoped (each session may be in a worktree)
    # ═══════════════════════════════════════════════════════════════

    def _session_root(sid: str) -> Path:
        """Get the workspace root for a session."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is not None and session.workspace_dir:
            return Path(session.workspace_dir).resolve()
        # Never expose the project root for a missing or uninitialized session.
        if session is None:
            raise HTTPException(404, "Session not found")
        ws = Path(app.state.data_dir) / "sessions" / sid
        ws.mkdir(parents=True, exist_ok=True)
        return ws.resolve()

    @app.get("/api/sessions/{sid}/files", response_model=FileTreeResponse)
    async def file_tree(
        sid: str,
        path: str = Query(".", description="Directory path relative to project root"),
        limit: int = Query(200, ge=1, le=500),
    ):
        """List files and directories in a session's workspace."""
        root = _session_root(sid)
        target = (root / path).resolve()
        # Security: ensure target is inside root.
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(403, "Path is outside session root")

        if not target.exists():
            raise HTTPException(404, f"Path not found: {path}")
        if not target.is_dir():
            raise HTTPException(400, "Path is not a directory")

        entries: list[FileEntry] = []
        try:
            items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            raise HTTPException(403, "Permission denied")

        for item in items[:limit]:
            try:
                rel = str(item.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            entries.append(FileEntry(
                name=item.name,
                path=rel,
                type="directory" if item.is_dir() else "file",
            ))

        return FileTreeResponse(
            path=path,
            entries=entries,
            truncated=len(list(target.iterdir())) > limit,
        )

    @app.get("/api/sessions/{sid}/files/content", response_model=FileContentResponse)
    async def file_content(
        sid: str,
        path: str = Query(..., description="File path relative to project root"),
        start_line: int = Query(1, ge=1),
        limit: int = Query(500, ge=1, le=2000),
    ):
        """Read file content from a session's workspace."""
        root = _session_root(sid)
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(403, "Path is outside session root")

        if not target.exists():
            raise HTTPException(404, f"File not found: {path}")
        if target.is_dir():
            raise HTTPException(400, "Path is a directory, not a file")

        # Quick binary check.
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "File appears to be binary")

        lines = text.splitlines()
        total = len(lines)
        start_idx = start_line - 1
        end_idx = min(start_idx + limit, total)
        selected = lines[start_idx:end_idx]

        # Format with line numbers.
        numbered = "\n".join(
            f"{i + 1}\t{line}" for i, line in enumerate(selected, start=start_idx)
        )

        return FileContentResponse(
            path=path,
            content=numbered,
            truncated=(end_idx < total),
        )

    @app.get("/api/sessions/{sid}/files/download")
    async def file_download(sid: str, path: str = Query(...)):
        """Download a file from the session workspace as attachment."""
        from fastapi.responses import FileResponse
        root = _session_root(sid)
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(403, "Path is outside session root")
        if not target.exists() or not target.is_file():
            raise HTTPException(404, f"File not found: {path}")
        return FileResponse(str(target), filename=target.name, media_type="application/octet-stream")

    # ═══════════════════════════════════════════════════════════════
    # Info — models, tools, MCP
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/models", response_model=list[ModelInfo])
    async def list_models():
        """List available LLM model profiles — always returns at least one."""
        from core.model_profiles import load_model_profiles
        profiles = load_model_profiles()
        result = [
            ModelInfo(
                name=p.name,
                model=p.model,
                provider=p.provider,
                base_url=p.base_url or "",
            )
            for p in profiles.values()
        ]
        if not result:
            # Fallback: return the current default model from env.
            import os
            result.append(ModelInfo(
                name="default",
                model=os.getenv("LLM_MODEL_ID", "deepseek-v4-flash"),
                provider=os.getenv("LLM_PROVIDER", "deepseek"),
                base_url=os.getenv("LLM_BASE_URL", ""),
            ))
        return result

    @app.get("/api/tools", response_model=list[ToolInfo])
    async def list_tools():
        """List registered tools (from shared tool registry)."""
        registry = app.state.tool_registry
        if registry is None:
            return []

        result: list[ToolInfo] = []
        for tool in registry.get_all_tools():
            params = []
            try:
                for p in tool.get_parameters():
                    params.append(ToolParam(
                        name=p.name,
                        type=p.type,
                        description=p.description,
                        required=p.required if hasattr(p, "required") else True,
                    ))
            except Exception:
                pass
            result.append(ToolInfo(
                name=tool.name,
                description=tool.description or "",
                parameters=params,
            ))
        return result

    @app.get("/api/mcp/status", response_model=McpStatusResponse)
    async def mcp_status():
        """Get MCP server connection status."""
        servers: list[McpServerStatus] = []
        pending: list[str] = []

        # Gather from active sessions.
        ctrl: SessionController = app.state.controller
        for sid in ctrl.list_sessions():
            session = ctrl.get_session(sid)
            if session is None or session._agent is None:
                continue
            agent = session._agent
            registry_tools = list(agent.tool_registry.get_all_tools()) if hasattr(agent, "tool_registry") else []
            for client in getattr(agent, "_mcp_clients", []):
                name = getattr(client.config, "name", "unknown") if hasattr(client, "config") else "unknown"
                connected = client.is_connected if hasattr(client, "is_connected") else False
                # Count tools registered for THIS mcp client (MCPToolAdapter stores its client)
                tool_count = sum(
                    1 for t in registry_tools
                    if getattr(t, "_mcp_client", None) is client
                )
                servers.append(McpServerStatus(
                    name=name,
                    connected=connected,
                    tool_count=tool_count,
                ))

        # Pending servers from loader.
        try:
            from tools.mcp.loader import get_pending_server_names, connect_mode
            pending = get_pending_server_names()
            mode = connect_mode()
        except Exception:
            mode = "unknown"

        return McpStatusResponse(servers=servers, pending=pending, connect_mode=mode)

    # ═══════════════════════════════════════════════════════════════
    # Skills — user-configurable skill management
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/skills", response_model=list[SkillInfo])
    async def list_skills():
        """List all project skills."""
        from core.skills.skill_loader import SkillLoader
        loader = SkillLoader(app.state.project_root)
        try:
            skills = loader.list_skills(refresh=True)
        except Exception:
            skills = []
        return [
            SkillInfo(name=s.name, description=s.description, base_dir=s.base_dir)
            for s in skills
        ]

    @app.get("/api/skills/{name}/content")
    async def get_skill_content(name: str):
        """Read a skill's SKILL.md file content."""
        from core.skills.skill_loader import SkillLoader
        loader = SkillLoader(app.state.project_root)
        skill = loader.get_skill(name, refresh=True)
        if skill is None:
            raise HTTPException(404, f"Skill not found: {name}")
        try:
            content = Path(skill.path).read_text(encoding="utf-8")
        except Exception:
            raise HTTPException(500, "Failed to read skill file")
        # Strip frontmatter to get body
        body = content.split("---\n", 2)[-1] if content.count("---") >= 2 else content
        # Re-parse frontmatter
        parts = content.split("---", 2)
        fm_lines = parts[1].strip().splitlines() if len(parts) >= 3 else []
        frontmatter = {}
        for line in fm_lines:
            if ":" in line:
                k, v = line.split(":", 1)
                frontmatter[k.strip()] = v.strip().strip('"')
        return {
            "name": skill.name,
            "description": skill.description,
            "content": body.strip(),
            "frontmatter": frontmatter,
        }

    @app.post("/api/skills", status_code=201)
    async def create_skill(body: SkillCreate):
        """Create a new skill — writes .mycodeagent/skills/<name>/SKILL.md."""
        skills_dir = Path(app.state.project_root) / ".mycodeagent" / "skills" / body.name
        if skills_dir.exists():
            raise HTTPException(409, f"Skill '{body.name}' already exists")
        try:
            skills_dir.mkdir(parents=True, exist_ok=False)
            frontmatter = f"---\nname: {body.name}\ndescription: \"{body.description}\"\n---\n"
            (skills_dir / "SKILL.md").write_text(
                frontmatter + "\n" + body.content + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise HTTPException(500, f"Failed to create skill: {exc}")
        return {"status": "created", "name": body.name}

    @app.put("/api/skills/{name}")
    async def update_skill(name: str, body: SkillUpdate):
        """Update an existing skill."""
        from core.skills.skill_loader import SkillLoader
        loader = SkillLoader(app.state.project_root)
        skill = loader.get_skill(name, refresh=True)
        if skill is None:
            raise HTTPException(404, f"Skill not found: {name}")
        try:
            current = Path(skill.path).read_text(encoding="utf-8")
        except Exception:
            raise HTTPException(500, "Failed to read skill file")

        # Update frontmatter description if provided
        if body.description is not None:
            parts = current.split("---", 2)
            if len(parts) >= 3:
                fm_lines = parts[1].strip().splitlines()
                new_fm_lines = []
                for line in fm_lines:
                    if line.strip().startswith("description:"):
                        new_fm_lines.append(f"description: \"{body.description}\"")
                    else:
                        new_fm_lines.append(line)
                parts[1] = "\n".join(new_fm_lines)
                current = "---".join(parts)

        # Update body if provided
        if body.content is not None:
            parts = current.split("---", 2)
            if len(parts) >= 3:
                current = "---".join(parts[:2]) + "---\n" + body.content + "\n"
            else:
                current = current.rstrip() + "\n" + body.content + "\n"

        try:
            Path(skill.path).write_text(current, encoding="utf-8")
        except Exception:
            raise HTTPException(500, "Failed to write skill file")
        return {"status": "updated", "name": name}

    @app.delete("/api/skills/{name}")
    async def delete_skill(name: str):
        """Delete a skill directory."""
        from core.skills.skill_loader import SkillLoader
        import shutil
        loader = SkillLoader(app.state.project_root)
        skill = loader.get_skill(name, refresh=True)
        if skill is None:
            raise HTTPException(404, f"Skill not found: {name}")
        try:
            shutil.rmtree(Path(skill.path).parent)
        except Exception as exc:
            raise HTTPException(500, f"Failed to delete skill: {exc}")
        return {"status": "deleted", "name": name}

    @app.post("/api/skills/validate")
    async def validate_skill(body: SkillCreate):
        """Validate a skill submission without creating it.

        Checks that the YAML frontmatter has required fields (name, description)
        and the content is non-empty markdown.
        """
        errors: list[str] = []
        if not body.name or not body.name.strip():
            errors.append("缺少 name 字段")
        elif not __import__('re').match(r'^[a-z0-9]+(?:-[a-z0-9]+)*$', body.name.strip()):
            errors.append("name 格式无效（只允许小写字母、数字、连字符）")
        if not body.description or not body.description.strip():
            errors.append("缺少 description 字段")
        if not body.content or not body.content.strip():
            errors.append("内容不能为空")
        return {"valid": len(errors) == 0, "errors": errors}

    # ── Enable/disable state ───────────────────────────────────────
    _state_path = Path(app.state.project_root) / "skill_state.json"

    def _read_state() -> dict:
        if not _state_path.exists():
            return {}
        try:
            import json
            return json.loads(_state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(data: dict) -> None:
        import json
        _state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @app.get("/api/skills/{name}/enabled")
    async def get_skill_enabled(name: str):
        state = _read_state()
        disabled = state.get("disabled_skills", {}).get(name, False)
        return {"name": name, "enabled": not disabled}

    @app.put("/api/skills/{name}/enabled")
    async def toggle_skill_enabled(name: str):
        state = _read_state()
        disabled = state.setdefault("disabled_skills", {})
        currently = disabled.get(name, False)
        if currently:
            del disabled[name]
        else:
            disabled[name] = True
        _write_state(state)
        return {"name": name, "enabled": currently}  # toggled

    @app.put("/api/mcp/servers/{name}/enabled")
    async def toggle_mcp_enabled(name: str):
        state = _read_state()
        disabled = state.setdefault("disabled_mcp", {})
        currently = disabled.get(name, False)
        if currently:
            del disabled[name]
        else:
            disabled[name] = True
        _write_state(state)
        return {"name": name, "enabled": currently}

    @app.get("/api/mcp/servers/{name}/enabled")
    async def get_mcp_enabled(name: str):
        state = _read_state()
        disabled = state.get("disabled_mcp", {}).get(name, False)
        return {"name": name, "enabled": not disabled}

    # ═══════════════════════════════════════════════════════════════
    # MCP servers — user-configurable MCP server management
    # ═══════════════════════════════════════════════════════════════

    _mcp_config_path = Path(app.state.project_root) / "mcp_servers.json"

    def _read_mcp_config() -> dict:
        if not _mcp_config_path.exists():
            return {"mcpServers": {}}
        try:
            import json
            return json.loads(_mcp_config_path.read_text(encoding="utf-8"))
        except Exception:
            return {"mcpServers": {}}

    def _write_mcp_config(data: dict) -> None:
        import json
        _mcp_config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @app.get("/api/mcp/servers", response_model=list[McpServerConfig])
    async def list_mcp_servers():
        """List configured MCP servers."""
        data = _read_mcp_config()
        servers = data.get("mcpServers", {})
        return [
            McpServerConfig(
                name=name,
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
            )
            for name, cfg in servers.items()
        ]

    @app.post("/api/mcp/servers", status_code=201)
    async def add_mcp_server(body: McpServerCreate):
        """Add a new MCP server configuration."""
        data = _read_mcp_config()
        servers = data.setdefault("mcpServers", {})
        if body.name in servers:
            raise HTTPException(409, f"MCP server '{body.name}' already exists")
        servers[body.name] = {
            "command": body.command,
            "args": body.args,
        }
        _write_mcp_config(data)
        return {"status": "created", "name": body.name}

    @app.put("/api/mcp/servers/{name}")
    async def update_mcp_server(name: str, body: McpServerUpdate):
        """Update an existing MCP server configuration."""
        data = _read_mcp_config()
        servers = data.get("mcpServers", {})
        if name not in servers:
            raise HTTPException(404, f"MCP server '{name}' not found")
        if body.command is not None:
            servers[name]["command"] = body.command
        if body.args is not None:
            servers[name]["args"] = body.args
        _write_mcp_config(data)
        return {"status": "updated", "name": name}

    @app.delete("/api/mcp/servers/{name}")
    async def remove_mcp_server(name: str):
        """Remove an MCP server configuration."""
        data = _read_mcp_config()
        servers = data.get("mcpServers", {})
        if name not in servers:
            raise HTTPException(404, f"MCP server '{name}' not found")
        del servers[name]
        _write_mcp_config(data)
        return {"status": "deleted", "name": name}

    # ═══════════════════════════════════════════════════════════════
    # Hooks — lifecycle hook management
    # ═══════════════════════════════════════════════════════════════

    _hooks_path = Path(app.state.project_root) / ".mycode" / "hooks.json"

    @app.get("/api/hooks")
    async def get_hooks():
        if not _hooks_path.exists():
            return {"hooks": {}}
        import json
        return json.loads(_hooks_path.read_text(encoding="utf-8"))

    @app.put("/api/hooks")
    async def save_hooks(body: dict):
        _hooks_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        _hooks_path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"status": "saved"}

    return app


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _sanitize_payload(payload: dict) -> dict:
    """Strip internal fields / large values that shouldn't go to frontend."""
    safe = {}
    for k, v in payload.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str) and len(v) > 50_000:
            safe[k] = v[:50_000] + "...(truncated)"
        elif isinstance(v, (str, int, float, bool, type(None), list, dict)):
            safe[k] = v
        else:
            safe[k] = str(v)
    return safe


# ═══════════════════════════════════════════════════════════════════════
# Direct-run entry point
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    # Quick-start: build a minimal agent for demonstration.
    from core.env import load_env
    load_env()

    from core.llm import HelloAgentsLLM
    from core.config import Config
    from tools.registry import ToolRegistry
    from agents.codeAgent import CodeAgent

    config = Config.from_env()
    llm = HelloAgentsLLM()
    tool_registry = ToolRegistry()

    def _make_agent() -> CodeAgent:
        return CodeAgent(
            name="web",
            llm=llm,
            tool_registry=tool_registry,
            project_root=os.getcwd(),
            config=config,
        )

    app = create_app(_make_agent, tool_registry, os.getcwd())
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
