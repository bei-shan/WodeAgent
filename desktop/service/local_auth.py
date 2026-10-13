"""Optional single-operator local Web authentication.

This is NOT multi-user RBAC. When WEB_AUTH_TOKEN is configured, browser clients
exchange it once for an expiring, HttpOnly SameSite cookie. Unsafe cookie
requests require a trusted Origin. WS handshakes check the same cookie and
Origin before acceptance. No query-string tokens or localStorage secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE_NAME = "wodeagent_session"
COOKIE_TTL = 8 * 3600
_ALLOWED_ORIGINS = frozenset({
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
})
_PUBLIC_PATHS = frozenset({"/api/auth/status", "/api/auth/login"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LocalWebAuth:
    def __init__(self, token: str):
        if len(token) < 24:
            raise ValueError("WEB_AUTH_TOKEN must contain at least 24 characters")
        self._token = token
        self._key = hashlib.sha256(("wodeagent-cookie-v1:" + token).encode()).digest()
        self._lock = threading.Lock()
        self._login_attempts: dict[str, deque[float]] = {}

    @staticmethod
    def valid_host(host: str | None) -> bool:
        if not host:
            return False
        try:
            u = urlsplit("http://" + host)
            return (
                u.hostname in {"localhost", "127.0.0.1", "::1"}
                and u.username is None and u.password is None
                and u.path in {"", "/"} and not u.query and not u.fragment
                and u.port in {None, 5173, 8000}
            )
        except ValueError:
            return False

    @staticmethod
    def valid_origin(origin: str | None) -> bool:
        return origin in _ALLOWED_ORIGINS

    def issue_cookie(self) -> str:
        issued = str(int(time.time()))
        nonce = secrets.token_hex(12)
        unsigned = f"v1.{issued}.{nonce}"
        sig = hmac.new(self._key, unsigned.encode(), hashlib.sha256).hexdigest()
        return f"{unsigned}.{sig}"

    def validate_cookie(self, cookie: str | None) -> bool:
        if not cookie or len(cookie) > 300:
            return False
        try:
            version, created, nonce, signature = cookie.split(".")
            if version != "v1" or not created.isascii() or not created.isdigit():
                return False
            issued = int(created)
            now = int(time.time())
            if issued > now + 60 or now - issued >= COOKIE_TTL or len(nonce) != 24 or len(signature) != 64:
                return False
            unsigned = f"v1.{created}.{nonce}"
            expected = hmac.new(self._key, unsigned.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def has_valid_bearer(self, auth: str | None) -> bool:
        if not auth or not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].encode(), self._token.encode())

    def authenticated(self, headers, cookies) -> bool:
        return self.has_valid_bearer(headers.get("authorization")) or self.validate_cookie(cookies.get(COOKIE_NAME))

    def allow_login_attempt(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            # Bound memory even if the client supplies numerous addresses.
            if len(self._login_attempts) >= 1024:
                self._login_attempts = {
                    k: q for k, q in self._login_attempts.items() if q and now - q[-1] < 60
                }
                if len(self._login_attempts) >= 1024 and address not in self._login_attempts:
                    return False
            attempts = self._login_attempts.setdefault(address, deque())
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return False
            attempts.append(now)
            return True

    def check_token(self, supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode(), self._token.encode())


class LocalAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth: LocalWebAuth):
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next):
        if not self.auth.valid_host(request.headers.get("host")):
            return JSONResponse({"detail": "Invalid local host"}, status_code=403)
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path not in _PUBLIC_PATHS:
            if not self.auth.authenticated(request.headers, request.cookies):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            if request.method in _UNSAFE_METHODS:
                origin = request.headers.get("origin")
                # Cookie-initiated mutations require an explicit trusted origin.
                # Bearer-only API clients without cookies may omit Origin.
                if not self.auth.valid_origin(origin) and not (
                    origin is None and self.auth.has_valid_bearer(request.headers.get("authorization"))
                ):
                    return JSONResponse({"detail": "Untrusted request origin"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp


def allow_websocket(ws: WebSocket, auth: LocalWebAuth | None) -> bool:
    if auth is None:
        return True
    if not auth.valid_host(ws.headers.get("host")):
        return False
    origin = ws.headers.get("origin")
    if not auth.valid_origin(origin) and not (
        origin is None and auth.has_valid_bearer(ws.headers.get("authorization"))
    ):
        return False
    return auth.authenticated(ws.headers, ws.cookies)
