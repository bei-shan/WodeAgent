"""Isolated hosted metadata API: never mounts local Agent Web routes.

This service only supports private session metadata CRUD. It does not run
Agents; allowing the legacy filesystem tools with a JWT would be an IDOR.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict

from core.persistence import PostgresPrivateSessionStore, SessionVersionConflict
from desktop.service.hosted_identity import HostedIdentity, InvalidPrincipal, OIDCSettings, OIDCVerifier


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
) -> FastAPI:
    """Separate factory and credentials from create_app() local trusted mode.

    Pass overrides only from trusted test/application wiring. Never permit
    arbitrary API requests to choose the database DSN, token verifier or scope.
    """
    if store is None:
        dsn = os.environ.get("RIDGE_POSTGRES_DSN", "").strip()
        if not dsn:
            raise RuntimeError("Hosted session API requires RIDGE_POSTGRES_DSN")
        store = PostgresPrivateSessionStore(dsn)
    if verifier is None:
        verifier = OIDCVerifier(OIDCSettings.from_env())

    app = FastAPI(title="Ridge Hosted Private Session API", version="0.1.0")
    app.state.store = store
    app.state.verifier = verifier

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
        try:
            import asyncio
            return await asyncio.to_thread(verifier.verify, token)
        except InvalidPrincipal:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

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
            if await asyncio.to_thread(store.production_probe):
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
