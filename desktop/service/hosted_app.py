"""Isolated hosted metadata API: never mounts local Agent Web routes.

This service only supports private session metadata CRUD. It does not run
Agents; allowing the legacy filesystem tools with a JWT would be an IDOR.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict

from core.persistence import PostgresPrivateSessionStore, SessionVersionConflict
from core.persistence.postgres_turn_queue import PostgresPrivateTurnQueue
from desktop.service.hosted_identity import HostedIdentity, InvalidPrincipal, OIDCSettings, OIDCVerifier


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimit:
    """ASGI ingress bound; protects JSON/Pydantic parsing under hostile load."""

    def __init__(self, app, max_bytes: int = 4_300_000):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except ValueError:
                declared = -1
            if declared < 0 or declared > self.max_bytes:
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(
                    scope, receive, send
                )
        length = 0

        async def bounded_receive():
            nonlocal length
            message = await receive()
            if message["type"] == "http.request":
                length += len(message.get("body", b""))
                if length > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            return await self.app(scope, bounded_receive, send)
        except _BodyTooLarge:
            return await JSONResponse({"detail": "Request too large"}, status_code=413)(
                scope, receive, send
            )


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=256)


class SessionSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=256)
    snapshot: dict[str, Any]


def create_hosted_app(
    *,
    verifier: OIDCVerifier | None = None,
    store: PostgresPrivateSessionStore | None = None,
    turn_events: PostgresPrivateTurnQueue | None = None,
) -> FastAPI:
    """Separate factory and credentials from create_app() local trusted mode.

    Pass overrides only from trusted test/application wiring. Never permit
    arbitrary API requests to choose the database DSN, token verifier or scope.
    """
    if store is None:
        dsn = os.environ.get("RIDGE_POSTGRES_DSN", "").strip()
        if not dsn:
            raise RuntimeError("Hosted session API requires RIDGE_POSTGRES_DSN")
        # In hosted mode refuse plaintext or unauthenticated server identity.
        # Test fixtures pass an explicitly injected local store instead.
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(dsn)
        tls = parse_qs(parsed.query).get("sslmode", [""])[0]
        if (parsed.scheme not in {"postgres", "postgresql"}
                or not parsed.hostname or tls != "verify-full"):
            raise RuntimeError(
                "Hosted PostgreSQL requires an URL DSN with sslmode=verify-full"
            )
        store = PostgresPrivateSessionStore(dsn)
    if verifier is None:
        verifier = OIDCVerifier(OIDCSettings.from_env())
    # Never couple test fakes to a global queue or infer an identity from
    # requests. Production metadata store and event store share one DB.
    if turn_events is None and type(store) is PostgresPrivateSessionStore:
        turn_events = PostgresPrivateTurnQueue(store._dsn)

    app = FastAPI(title="Ridge Hosted Private Session API", version="0.1.0")
    app.add_middleware(RequestBodyLimit)

    app.state.store = store
    app.state.verifier = verifier
    app.state.turn_events = turn_events

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def authenticated(authorization: str | None = Header(default=None)) -> HostedIdentity:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer authorization required")
        token = authorization[len("Bearer "):]
        if not token or " " in token:
            raise HTTPException(status_code=401, detail="Invalid bearer authorization")
        import asyncio
        try:
            identity = await asyncio.to_thread(verifier.verify, token)
        except InvalidPrincipal:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        # A cryptographically valid token does not confer permanent membership.
        # Read the authoritative, forced-RLS membership row on every request;
        # never cache permissions for the token's remaining lifetime.
        try:
            is_member = await asyncio.to_thread(
                store.authorize_membership, identity.scope
            )
        except Exception:
            # Missing table, revoked DB credentials or a network outage must
            # NEVER silently fall back to signed claims alone.
            raise HTTPException(
                status_code=503, detail="Authorization store unavailable"
            )
        if not is_member:
            raise HTTPException(status_code=403, detail="Project membership required")
        return identity

    def project(record):
        return {"id": record.id, "title": record.title,
                "snapshot": record.snapshot, "version": record.version}

    @app.get("/api/health/live")
    async def liveness():
        return {"status": "alive"}

    @app.get("/api/health/ready")
    async def readiness():
        """Fail closed if DB, runtime role or row-level protection is missing."""
        import asyncio
        try:
            if (await asyncio.to_thread(store.production_probe)
                    and turn_events is not None
                    and await asyncio.to_thread(turn_events.event_security_probe)):
                return {"status": "ready"}
        except Exception:
            pass
        raise HTTPException(503, "Storage security check failed")

    @app.get("/api/v2/sessions")
    async def list_sessions(identity: HostedIdentity = Depends(authenticated)):
        import asyncio
        records = await asyncio.to_thread(store.list_private, identity.scope)
        return [{"id": r.id, "title": r.title, "version": r.version} for r in records]

    @app.post("/api/v2/sessions", status_code=201)
    async def create_session(body: SessionCreateRequest,
                             identity: HostedIdentity = Depends(authenticated)):
        import asyncio
        record = await asyncio.to_thread(
            store.create, identity.scope, uuid.uuid4().hex[:12], title=body.title,
        )
        return project(record)

    @app.get("/api/v2/sessions/{sid}")
    async def get_session(sid: str, identity: HostedIdentity = Depends(authenticated)):
        import asyncio
        try:
            record = await asyncio.to_thread(store.get, identity.scope, sid)
        except ValueError:
            raise HTTPException(404, "Session not found")
        if record is None:
            raise HTTPException(404, "Session not found")
        return project(record)

    @app.put("/api/v2/sessions/{sid}")
    async def save_session(sid: str, body: SessionSaveRequest,
                           identity: HostedIdentity = Depends(authenticated)):
        import asyncio
        try:
            new_version = await asyncio.to_thread(
                store.save, identity.scope, sid, body.snapshot,
                expected_version=body.expected_version, title=body.title,
            )
        except SessionVersionConflict:
            raise HTTPException(409, "Session changed or is not available")
        except ValueError:
            raise HTTPException(404, "Session not found")
        return {"id": sid, "version": new_version}

    async def _owned_event_page(sid: str, identity: HostedIdentity,
                                after: int, limit: int) -> list[dict[str, Any]]:
        """Same OIDC, live membership and session ownership as metadata routes."""
        import asyncio
        if turn_events is None:
            raise HTTPException(503, "Hosted event ledger is not configured")
        try:
            session = await asyncio.to_thread(store.get, identity.scope, sid)
        except ValueError:
            raise HTTPException(404, "Session not found")
        if session is None:
            raise HTTPException(404, "Session not found")
        try:
            # Revalidate membership immediately before event retrieval, not
            # only when a connection initially authenticated.
            if await asyncio.to_thread(store.authorize_membership, identity.scope) is not True:
                raise HTTPException(403, "Project membership required")
            return await asyncio.to_thread(
                turn_events.events, identity.scope, sid, after=after, limit=limit,
            )
        except LookupError:
            raise HTTPException(404, "Session not found")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Durable event ledger unavailable")

    @app.get("/api/v2/sessions/{sid}/events")
    async def replay_events(
        sid: str,
        after: int = Query(default=0, ge=0, le=9223372036854775807),
        limit: int = Query(default=100, ge=1, le=100),
        identity: HostedIdentity = Depends(authenticated),
    ):
        """Stateless finite cursor replay; does NOT enqueue or run any tools."""
        return await _owned_event_page(sid, identity, after, limit)

    @app.get("/api/v2/sessions/{sid}/events/stream")
    async def replay_event_stream(
        sid: str,
        after: int = Query(default=0, ge=0, le=9223372036854775807),
        limit: int = Query(default=100, ge=1, le=100),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        identity: HostedIdentity = Depends(authenticated),
    ):
        """Finite SSE replay, with standard Last-Event-ID reconnect support.

        A caller reconnects after the response ends. This is NOT an infinite
        stream, changefeed listener or multi-process notification service.
        """
        import json
        if last_event_id is not None:
            if (not 1 <= len(last_event_id) <= 19
                    or not last_event_id.isascii() or not last_event_id.isdecimal()):
                raise HTTPException(400, "Invalid Last-Event-ID")
            resumed = int(last_event_id)
            if resumed > 9223372036854775807 or (after and after != resumed):
                raise HTTPException(400, "Conflicting event cursor")
            after = resumed
        events = await _owned_event_page(sid, identity, after, limit)
        frames = [
            f"id: {event['id']}\nevent: turn-state\ndata: "
            + json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n\n"
            for event in events
        ]
        if not frames:
            frames = [": no-new-events\n\n"]
        return StreamingResponse(
            iter(frames), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.delete("/api/v2/sessions/{sid}", status_code=204)
    async def delete_session(sid: str, identity: HostedIdentity = Depends(authenticated)):
        import asyncio
        try:
            deleted = await asyncio.to_thread(store.delete, identity.scope, sid)
        except ValueError:
            raise HTTPException(404, "Session not found")
        if not deleted:
            raise HTTPException(404, "Session not found")
        return None

    return app
