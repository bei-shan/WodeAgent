"""Ridge Code Web Service — FastAPI application.

Usage::

    from desktop.service.app import create_app
    app = create_app(agent_factory, tool_registry, project_root)
    # uvicorn desktop.service.app:app --host 127.0.0.1 --port 8000

Or run directly::

    python -m desktop.service.app
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
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
from desktop.service.application import (
    ServiceError, SessionService, TurnService, SessionSettingsService, TeamsService,
    WorkspaceService, ConfigCatalogService, SkillCatalogService,
)
from desktop.service.application.session_portability import MAX_BYTES
from desktop.service.application.errors import InvalidInputError, PayloadTooLargeError
from core.runtime.execution_history import ExecutionHistory
from desktop.service.application.git_diff import GitDiffService
from desktop.service.schemas import (
    SessionCreate,
    SessionInfo,
    SessionFork,
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
        Optional registry for tool listing. Default agents always own separate
        registries; the listing also includes initialized sessions' metadata.
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
                # Tools carry workspace, permission, interaction and discovery
                # state. Sharing this registry would rebind earlier sessions
                # to whichever session initialized last.
                tool_registry=ToolRegistry(), project_root=project_root,
                config=config,
            )

    app = FastAPI(
        title="Ridge Code API",
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

    # Old local workspace / filesystem routes are fundamentally single-user.
    # Disallow accidentally exposing them as a hosted OIDC service.
    if os.getenv("RIDGE_DEPLOYMENT_MODE", "local").strip().lower() == "hosted":
        raise RuntimeError(
            "Legacy local Agent API cannot run in hosted mode; "
            "use desktop.service.hosted_app:create_hosted_app instead"
        )

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
    # Single-process admission only: distributed deployments need shared leases.
    raw_turn_limit = os.getenv("RIDGE_MAX_ACTIVE_TURNS", "8")
    try:
        max_active_turns = int(raw_turn_limit)
    except ValueError as exc:
        raise ValueError("RIDGE_MAX_ACTIVE_TURNS must be an integer in 1..256") from exc
    app.state.controller = SessionController(
        workspace_base=os.path.join(app.state.data_dir, "sessions"),
        max_active_turns=max_active_turns,
    )
    app.state.agent_factory = agent_factory
    app.state.session_service = SessionService(
        controller=app.state.controller, agent_factory=agent_factory,
        data_dir=app.state.data_dir,
    )
    app.state.turn_service = TurnService(controller=app.state.controller)
    app.state.settings_service = SessionSettingsService(controller=app.state.controller)
    app.state.teams_service = TeamsService(controller=app.state.controller)
    app.state.workspace_service = WorkspaceService(
        controller=app.state.controller, data_dir=app.state.data_dir,
    )
    app.state.execution_history = ExecutionHistory(os.path.join(app.state.data_dir, 'sessions'))
    app.state.git_diff_service = GitDiffService(app.state.workspace_service)
    # Interactive containers are opt-in, require explicit local auth, and are
    # capped independently from Agent turn admission.
    app.state.terminal_enabled = os.getenv('WEB_SANDBOX_TERMINAL_ENABLED', '').lower() == 'true' and auth is not None
    app.state.terminal_sessions = set()
    app.state.tool_registry = tool_registry
    app.state.project_root = os.path.abspath(project_root)
    app.state.config_catalog = ConfigCatalogService(app.state.project_root)
    app.state.skill_catalog = SkillCatalogService(app.state.project_root)
    from desktop.service.application.plugin_catalog import PluginCatalogService
    app.state.plugin_catalog = PluginCatalogService(app.state.project_root)
    app.state._started_at = time.time()

    # ── Lifespan ──────────────────────────────────────────────────
    @app.exception_handler(ServiceError)
    async def _application_error(_request: Request, exc: ServiceError):
        status = {"not_found": 404, "conflict": 409, "invalid_input": 400, "forbidden": 403, "too_large": 413, "capacity": 429}.get(exc.kind, 500)
        headers = {"Retry-After": "1"} if exc.kind == "capacity" else None
        return JSONResponse(
            status_code=status,
            content={"detail": str(exc) if status < 500 else "Internal error"},
            headers=headers,
        )

    async def _upload_gc_loop():
        # Files and grants may predate this server process. The GC uses only
        # descriptor-anchored existing session paths, never arbitrary client
        # strings, and never recreates deleted workspaces.
        while True:
            try:
                await asyncio.to_thread(app.state.workspace_service.cleanup_expired_uploads)
            except Exception:
                logger.exception("Attachment GC iteration failed")
            await asyncio.sleep(3600)

    @app.on_event("startup")
    async def _startup():
        app.state.upload_gc_task = asyncio.create_task(
            _upload_gc_loop(), name="ridge-upload-gc"
        )
        logger.info("Service started — project_root=%s", app.state.project_root)

    @app.on_event("shutdown")
    async def _shutdown():
        gc_task = getattr(app.state, "upload_gc_task", None)
        if gc_task is not None:
            gc_task.cancel()
            await asyncio.gather(gc_task, return_exceptions=True)
        ctrl: SessionController = app.state.controller
        # Do not close shared MCP clients or delete worktrees while a worker
        # is executing. Keep the event loop responsive during the bounded drain.
        result = await asyncio.to_thread(ctrl.shutdown, grace_seconds=2.0)
        if result["running"]:
            logger.warning(
                "Service shutdown: %s session(s) did not stop within grace period",
                result["running"],
            )
        logger.info("Service shut down; closed %s idle session(s)", result["closed"])

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
        usage = ctrl.admission.snapshot()
        return {
            "status": "ok",
            "uptime": int(time.time() - app.state._started_at),
            "sessions": len(ctrl.list_sessions()),
            "turn_admission": {
                "active": usage.active,
                "limit": usage.limit,
                "rejected": usage.rejected,
            },
        }

    # ═══════════════════════════════════════════════════════════════
    # Sessions
    # ═══════════════════════════════════════════════════════════════

    @app.post("/api/sessions", response_model=SessionInfo, status_code=201)
    async def create_session(body: SessionCreate):
        return app.state.session_service.create(body.title)

    @app.post("/api/sessions/import", response_model=SessionInfo, status_code=201)
    async def import_portable_session(request: Request):
        """Import a bounded, validated portable conversation archive."""
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise InvalidInputError("Content-Type must be application/json")
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BYTES:
                raise PayloadTooLargeError("Portable session exceeds 1 MiB")
        try:
            def unique_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON keys are not allowed")
                    result[key] = value
                return result
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_keys)
        except (UnicodeDecodeError, ValueError) as exc:
            raise InvalidInputError("Invalid portable session JSON") from exc
        return app.state.session_service.import_portable(payload)

    @app.get("/api/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        return app.state.session_service.list()

    @app.get("/api/sessions/{sid}", response_model=SessionInfo)
    async def get_session(sid: str):
        return app.state.session_service.get(sid)

    @app.post("/api/sessions/{sid}/activate")
    async def activate_session(sid: str):
        return app.state.session_service.activate(sid)

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        response = app.state.session_service.delete(sid)
        await asyncio.to_thread(app.state.workspace_service.revoke_session_uploads, sid)
        return response

    @app.put("/api/sessions/{sid}/rename")
    async def rename_session(sid: str, body: dict):
        return app.state.session_service.rename(sid, body.get("title") or "")

    @app.post("/api/sessions/{sid}/pin")
    async def toggle_pin_session(sid: str):
        return app.state.session_service.toggle_pin(sid)

    @app.get("/api/sessions/{sid}/history")
    async def get_session_history(sid: str):
        return app.state.session_service.history(sid)

    @app.get("/api/sessions/{sid}/export")
    async def export_portable_session(sid: str):
        data = app.state.session_service.export_portable(sid)
        return JSONResponse(content=data, headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{sid}/trace")
    async def get_session_trace(sid: str):
        """Expose a safe projection of a trusted session trace, never raw JSONL."""
        from desktop.service.application.trace_analysis import analyze_trace
        app.state.session_service._require_known(sid)
        session = app.state.controller.get_session(sid)
        agent = getattr(session, "_agent", None) if session is not None else None
        tracer = getattr(agent, "trace_logger", None)
        trace_file = getattr(tracer, "_filepath", None)
        trace_id = getattr(tracer, "session_id", None)
        empty = {"available": False, "trace_id": None, "truncated": False,
                 "pricing_configured": False, "runs": []}
        if trace_file is None or not isinstance(trace_id, str):
            return empty
        path = Path(trace_file)
        if (path.name != f"trace-{trace_id}.jsonl"
                or path.parent.resolve() != Path(tracer.trace_dir).resolve()
                or path.is_symlink()):
            return empty
        return await asyncio.to_thread(analyze_trace, path, trace_id)

    @app.get("/api/sessions/{sid}/tree")
    async def get_session_tree(sid: str):
        return app.state.session_service.tree(sid)

    @app.post("/api/sessions/{sid}/fork")
    async def fork_session_tree(sid: str, body: SessionFork):
        return app.state.session_service.fork(sid, body.target_id)

    # ═══════════════════════════════════════════════════════════
    # Turns
    # ═══════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/turn")
    async def get_turn_status(sid: str):
        return app.state.turn_service.status(sid)

    @app.post("/api/sessions/{sid}/messages")
    async def send_message(sid: str, body: MessageSend):
        # Resolve every attachment through this session's anchored workspace
        # before any new Agent turn is admitted. Client-provided paths alone
        # are never accepted as media authorization.
        if not body.content.strip() and not body.attachments:
            raise InvalidInputError("Message text or attachment is required")
        parts = await asyncio.to_thread(
            app.state.workspace_service.prepare_attachments, sid, body.attachments
        )
        return await asyncio.to_thread(
            app.state.turn_service.send, sid, body.content, parts,
        )

    @app.post("/api/sessions/{sid}/interrupt")
    async def interrupt(sid: str):
        return app.state.turn_service.interrupt(sid)

    @app.post("/api/sessions/{sid}/upload")
    async def upload_file(sid: str, file: UploadFile = File(...)):
        """Stream an upload into the session workspace (max 25 MiB)."""
        return await asyncio.to_thread(
            app.state.workspace_service.upload, sid, file.filename or "upload", file.file,
        )

    # ═══════════════════════════════════════════════════════════════
    # Permissions
    # ═══════════════════════════════════════════════════════════════

    @app.post("/api/sessions/{sid}/permissions/{rid}/resolve")
    async def resolve_permission(sid: str, rid: str, body: PermissionResolve):
        return app.state.turn_service.resolve_permission(sid, rid, body.decision)

    @app.post("/api/sessions/{sid}/ask-user/{rid}/answer")
    async def answer_ask_user(sid: str, rid: str, body: AskUserAnswer):
        return app.state.turn_service.answer_ask_user(sid, rid, body.answer)

    # ═══════════════════════════════════════════════════════════
    # Session config
    # ═══════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/config", response_model=SessionConfig)
    async def get_session_config(sid: str):
        return app.state.settings_service.get(sid)

    @app.put("/api/sessions/{sid}/config")
    async def update_session_config(sid: str, body: SessionConfigUpdate):
        return app.state.settings_service.update(
            sid, model_profile=body.model_profile,
            enable_agent_teams=body.enable_agent_teams,
            plan_mode=body.plan_mode,
        )

    # ═══════════════════════════════════════════════════════════
    # Agent teams
    # ═══════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/teams", response_model=list[TeamInfo])
    async def list_teams(sid: str):
        return app.state.teams_service.list(sid)

    @app.post("/api/sessions/{sid}/teams", status_code=201)
    async def create_team(sid: str, body: TeamCreate):
        return app.state.teams_service.create(sid, body.team_name)

    # ═══════════════════════════════════════════════════════════
    # WebSocket — event stream
    # ═══════════════════════════════════════════════════════════

    @app.get("/api/sessions/{sid}/events")
    async def session_sse_events(
        request: Request, sid: str, after: str | None = Query(None),
    ):
        """SSE with Last-Event-ID replay; complements the WebSocket endpoint."""
        from desktop.service.sse_stream import parse_event_cursor, follow_events

        session = app.state.controller.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        raw_header = request.headers.get("last-event-id")
        raw = raw_header if raw_header is not None else after
        try:
            cursor, reset_reason = parse_event_cursor(raw, session.events, busy=session.busy)
        except ValueError as exc:
            raise InvalidInputError("Invalid event cursor") from exc
        # Unsequenced prompts are only restored for a fresh subscriber.
        pending = tuple(session.pending_prompts()) if raw is None else ()
        return StreamingResponse(
            follow_events(
                session.events, cursor=cursor, reset_reason=reset_reason,
                events=_STREAM_EVENTS, sanitize=_sanitize_payload,
                disconnected=request.is_disconnected, pending=pending,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

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

    @app.get("/api/sessions/{sid}/files", response_model=FileTreeResponse)
    async def file_tree(
        sid: str,
        path: str = Query(".", description="Directory path relative to session root"),
        limit: int = Query(200, ge=1, le=500),
    ):
        return await asyncio.to_thread(app.state.workspace_service.list, sid, path, limit)

    @app.get("/api/sessions/{sid}/files/content", response_model=FileContentResponse)
    async def file_content(
        sid: str,
        path: str = Query(..., description="File path relative to session root"),
        start_line: int = Query(1, ge=1),
        limit: int = Query(500, ge=1, le=2000),
    ):
        return await asyncio.to_thread(
            app.state.workspace_service.content, sid, path, start_line, limit,
        )

    @app.get("/api/sandbox/terminal-capabilities")
    async def sandbox_terminal_capabilities():
        import shutil
        return {
            "enabled": bool(app.state.terminal_enabled and shutil.which("docker")),
            "backend": "docker",
            "interactive": True,
        }

    @app.websocket("/api/sessions/{sid}/terminal")
    async def session_docker_terminal(ws: WebSocket, sid: str):
        # Refuse when local auth is absent even if the UI is running on
        # localhost. A browser WebSocket has no CSRF token of its own.
        if not app.state.terminal_enabled or not allow_websocket(ws, auth):
            await ws.close(code=4403)
            return
        try:
            app.state.session_service._require_known(sid)
        except ServiceError:
            await ws.close(code=4404)
            return
        if sid in app.state.terminal_sessions or len(app.state.terminal_sessions) >= 2:
            await ws.close(code=4429)
            return
        app.state.terminal_sessions.add(sid)
        try:
            from core.sandbox.docker_provider import DockerCommandProvider
            from desktop.service.docker_terminal import serve_docker_terminal
            from core.sandbox.contracts import SandboxUnavailable
            import shutil
            if not shutil.which("docker"):
                await ws.close(code=4503)
                return
            # The secure workspace service materializes and verifies the
            # per-session directory without following a session symlink.
            with app.state.workspace_service._root_fd(sid):
                workspace = os.path.join(app.state.data_dir, "sessions", sid)
                provider = DockerCommandProvider(workspace)
            await serve_docker_terminal(ws, provider)
        except (SandboxUnavailable, ServiceError, OSError):
            await ws.close(code=4503)
        finally:
            app.state.terminal_sessions.discard(sid)

    @app.get("/api/sessions/{sid}/git/diff")
    async def get_session_git_diff(
        sid: str,
        path: str = Query(..., description="File relative to session workspace"),
    ):
        """Tracked worktree changes against HEAD; never host/project fallback."""
        return await asyncio.to_thread(app.state.git_diff_service.diff, sid, path)

    @app.get("/api/sessions/{sid}/execution-history")
    async def get_session_execution_history(sid: str):
        """Bounded private tool-event summaries, including after restart."""
        app.state.session_service._require_known(sid)
        return await asyncio.to_thread(app.state.execution_history.list, sid)

    @app.get("/api/sessions/{sid}/files/download")
    async def file_download(sid: str, path: str = Query(...)):
        filename, handle = await asyncio.to_thread(
            app.state.workspace_service.download, sid, path,
        )

        def chunks():
            try:
                while data := handle.read(65536):
                    yield data
            finally:
                handle.close()

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

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
        """Project live tool metadata without sharing session executors."""
        registries = []
        if app.state.tool_registry is not None:
            registries.append(app.state.tool_registry)
        ctrl: SessionController = app.state.controller
        for sid in ctrl.list_sessions():
            session = ctrl.get_session(sid)
            # Reading the catalog must not initialize models or MCP clients.
            agent = session.agent if session is not None else None
            registry = getattr(agent, "tool_registry", None)
            if registry is not None and not any(registry is item for item in registries):
                registries.append(registry)

        result: list[ToolInfo] = []
        seen_names: set[str] = set()
        tools = []
        for registry in registries:
            get_tools = getattr(registry, "get_all_tools", None)
            if callable(get_tools):
                tools.extend(get_tools())
        for tool in tools:
            if tool.name in seen_names:
                continue
            seen_names.add(tool.name)
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

        # Gather from initialized sessions. Custom factories can share an MCP
        # transport, so report each actual client once, without sharing tools.
        ctrl: SessionController = app.state.controller
        seen_clients: list[Any] = []
        for sid in ctrl.list_sessions():
            session = ctrl.get_session(sid)
            if session is None or session._agent is None:
                continue
            agent = session._agent
            registry_tools = list(agent.tool_registry.get_all_tools()) if hasattr(agent, "tool_registry") else []
            for client in getattr(agent, "_mcp_clients", []):
                if any(client is prior for prior in seen_clients):
                    continue
                seen_clients.append(client)
                name = (getattr(client.config, "name", "") or "unknown") if hasattr(client, "config") else "unknown"
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
        return app.state.skill_catalog.list()

    @app.get("/api/skills/{name}/content")
    async def get_skill_content(name: str):
        return app.state.skill_catalog.get(name)

    @app.post("/api/skills", status_code=201)
    async def create_skill(body: SkillCreate):
        return app.state.skill_catalog.create(body.name, body.description, body.content)

    @app.put("/api/skills/{name}")
    async def update_skill(name: str, body: SkillUpdate):
        return app.state.skill_catalog.update(name, body.description, body.content)

    @app.delete("/api/skills/{name}")
    async def delete_skill(name: str):
        return app.state.skill_catalog.delete(name)

    @app.post("/api/skills/validate")
    async def validate_skill(body: SkillCreate):
        return app.state.skill_catalog.validate(body.name, body.description, body.content)

    # ── Installable project plugins ───────────────────────────────

    @app.get("/api/plugins")
    async def list_plugins():
        return app.state.plugin_catalog.list()

    @app.post("/api/plugins/install", status_code=201)
    async def install_plugin(file: UploadFile = File(...)):
        # The handler never consumes an unbounded body and never fetches a
        # plugin from a user-supplied URL. Auth middleware protects the route.
        data = await file.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise PayloadTooLargeError("Plugin ZIP exceeds 2 MiB")
        return app.state.plugin_catalog.install(data)

    @app.post("/api/plugins/{name}/update")
    async def update_plugin(name: str, file: UploadFile = File(...), sha256: str | None = Query(None)):
        # Identical read budget to initial install; no URL-based fetch.
        data = await file.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise PayloadTooLargeError("Plugin ZIP exceeds 2 MiB")
        return app.state.plugin_catalog.update_zip(name, data, sha256)

    @app.put("/api/plugins/{name}/pinned")
    async def pin_plugin(name: str, body: dict):
        return app.state.plugin_catalog.pin(name, body.get("pinned"))

    @app.post("/api/plugins/{name}/rollback")
    async def rollback_plugin(name: str):
        return app.state.plugin_catalog.rollback(name)

    @app.put("/api/plugins/{name}/enabled")
    async def enable_plugin(name: str, body: dict):
        return app.state.plugin_catalog.enable(name, body.get("enabled"))

    @app.put("/api/plugins/{name}/trusted")
    async def trust_plugin(name: str, body: dict):
        # This is still gated by RIDGE_ALLOW_PLUGIN_CODE on the backend.
        return app.state.plugin_catalog.trust(name, body.get("trusted"))

    @app.delete("/api/plugins/{name}")
    async def uninstall_plugin(name: str):
        return app.state.plugin_catalog.delete(name)

    # ── Enable/disable state ───────────────────────────────────────

    @app.get("/api/skills/{name}/enabled")
    async def get_skill_enabled(name: str):
        return app.state.config_catalog.enabled("skill", name)

    @app.put("/api/skills/{name}/enabled")
    async def toggle_skill_enabled(name: str):
        return app.state.config_catalog.toggle_enabled("skill", name)

    @app.put("/api/mcp/servers/{name}/enabled")
    async def toggle_mcp_enabled(name: str):
        return app.state.config_catalog.toggle_enabled("mcp", name)

    @app.get("/api/mcp/servers/{name}/enabled")
    async def get_mcp_enabled(name: str):
        return app.state.config_catalog.enabled("mcp", name)

    # ═══════════════════════════════════════════════════════════════
    # MCP servers — user-configurable MCP server management
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/mcp/servers", response_model=list[McpServerConfig])
    async def list_mcp_servers():
        return app.state.config_catalog.list_mcp()

    @app.post("/api/mcp/servers", status_code=201)
    async def add_mcp_server(body: McpServerCreate):
        return app.state.config_catalog.create_mcp(body.name, body.command, body.args)

    @app.put("/api/mcp/servers/{name}")
    async def update_mcp_server(name: str, body: McpServerUpdate):
        return app.state.config_catalog.update_mcp(name, body.command, body.args)

    @app.delete("/api/mcp/servers/{name}")
    async def remove_mcp_server(name: str):
        return app.state.config_catalog.delete_mcp(name)

    # ═══════════════════════════════════════════════════════════════
    # Hooks — lifecycle hook management
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/hooks")
    async def get_hooks():
        return app.state.config_catalog.get_hooks()

    @app.put("/api/hooks")
    async def save_hooks(body: dict):
        return app.state.config_catalog.save_hooks(body)

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

    # Keep module launches on the same session-owned factory as the CLI.
    app = create_app(project_root=os.getcwd())
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
