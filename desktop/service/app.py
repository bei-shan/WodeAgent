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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware

# Ensure project root on path.
_proj = Path(__file__).resolve().parents[2]
if str(_proj) not in sys.path:
    sys.path.insert(0, str(_proj))

from core.runtime.session_controller import (
    SessionController,
    SESSION_EVENT_PERMISSION,
    SESSION_EVENT_ASK_USER,
    SESSION_EVENT_TURN_DONE,
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
)

logger = logging.getLogger("mycodeagent.service")

# ── Event types forwarded to frontend ─────────────────────────────────
_STREAM_EVENTS = {
    EventType.RUN_STARTED,
    EventType.RUN_FINISHED,
    EventType.STEP_STARTED,
    EventType.LLM_STARTED,
    EventType.LLM_COMPLETED,
    EventType.TOOL_STARTED,
    EventType.TOOL_COMPLETED,
    EventType.ASSISTANT_FINAL,
    SESSION_EVENT_PERMISSION,
    SESSION_EVENT_ASK_USER,
    SESSION_EVENT_TURN_DONE,
    SESSION_EVENT_ERROR,
}


# ═══════════════════════════════════════════════════════════════════════
# App factory
# ═══════════════════════════════════════════════════════════════════════

def create_app(
    agent_factory: Callable[[], Any],
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

    app = FastAPI(
        title="MyCodeAgent API",
        version="0.1.0",
        docs_url="/docs",
    )

    # CORS — permissive for local dev; tighten for production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Shared state ──────────────────────────────────────────────
    app.state.controller = SessionController()
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
        return SessionInfo(id=sid, title=body.title or sid[:8], busy=False)

    @app.get("/api/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        """List all active sessions."""
        ctrl: SessionController = app.state.controller
        return [
            SessionInfo(
                id=sid,
                title=sid[:8],
                busy=(ctrl.get_session(sid).busy if ctrl.get_session(sid) else False),
            )
            for sid in ctrl.list_sessions()
        ]

    @app.get("/api/sessions/{sid}", response_model=SessionInfo)
    async def get_session(sid: str):
        """Get session info."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        return SessionInfo(id=sid, title=sid[:8], busy=session.busy)

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        """Delete a session and free its resources."""
        ctrl: SessionController = app.state.controller
        if not ctrl.delete_session(sid):
            raise HTTPException(404, "Session not found")
        return {"status": "deleted"}

    # ═══════════════════════════════════════════════════════════════
    # Messages
    # ═══════════════════════════════════════════════════════════════

    @app.post("/api/sessions/{sid}/messages")
    async def send_message(sid: str, body: MessageSend):
        """Send a user message — starts an agent turn in a worker thread."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        ok = session.send_message(body.content)
        if not ok:
            raise HTTPException(409, "Agent is busy — wait for turn.completed event")
        return {"status": "accepted"}

    @app.post("/api/sessions/{sid}/interrupt")
    async def interrupt(sid: str):
        """Request the agent to stop (best-effort)."""
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        session.interrupt()
        return {"status": "interrupted"}

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
    # WebSocket — event stream
    # ═══════════════════════════════════════════════════════════════

    @app.websocket("/api/sessions/{sid}/stream")
    async def event_stream(ws: WebSocket, sid: str):
        """Stream agent events to the frontend in real time."""
        await ws.accept()

        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is None:
            await ws.send_json({"type": "error", "payload": {"message": "Session not found"}})
            await ws.close()
            return

        # Bridge sync queue → async WebSocket.
        loop = asyncio.get_event_loop()
        try:
            while True:
                # Check for client disconnect (non-blocking).
                try:
                    event: AgentEvent = await loop.run_in_executor(
                        None, session.events.get, True, 1.0  # block=True, timeout=1s
                    )
                except Exception:
                    # queue.Empty after 1s — just loop to check disconnect
                    continue

                if event is None:
                    continue

                # Forward relevant events to frontend.
                if event.type in _STREAM_EVENTS:
                    await ws.send_json({
                        "type": event.type,
                        "payload": _sanitize_payload(event.payload),
                        "step": event.step,
                    })

        except WebSocketDisconnect:
            pass  # client closed — clean exit
        except Exception:
            logger.exception("WebSocket error for session %s", sid[:8])
        finally:
            # Don't delete session on disconnect — user may reconnect.
            pass

    # ═══════════════════════════════════════════════════════════════
    # Files — session-scoped (each session may be in a worktree)
    # ═══════════════════════════════════════════════════════════════

    def _session_root(sid: str) -> Path:
        """Get the project root for a session (may differ due to worktree)."""
        # If session has an active agent, use its project_root.
        # Otherwise fall back to the shared base root.
        ctrl: SessionController = app.state.controller
        session = ctrl.get_session(sid)
        if session is not None and session._agent is not None:
            return Path(session._agent.project_root).resolve()
        return Path(app.state.project_root).resolve()

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

    # ═══════════════════════════════════════════════════════════════
    # Info — models, tools, MCP
    # ═══════════════════════════════════════════════════════════════

    @app.get("/api/models", response_model=list[ModelInfo])
    async def list_models():
        """List available LLM model profiles."""
        from core.model_profiles import load_model_profiles
        profiles = load_model_profiles()
        return [
            ModelInfo(
                name=p.name,
                model=p.model,
                provider=p.provider,
                base_url=p.base_url or "",
            )
            for p in profiles.values()
        ]

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
            for client in getattr(agent, "_mcp_clients", []):
                name = getattr(client.config, "name", "unknown") if hasattr(client, "config") else "unknown"
                connected = client.is_connected if hasattr(client, "is_connected") else False
                servers.append(McpServerStatus(
                    name=name,
                    connected=connected,
                    tool_count=0,  # TODO: count registered MCP tools
                ))

        # Pending servers from loader.
        try:
            from tools.mcp.loader import get_pending_server_names, connect_mode
            pending = get_pending_server_names()
            mode = connect_mode()
        except Exception:
            mode = "unknown"

        return McpStatusResponse(servers=servers, pending=pending, connect_mode=mode)

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
