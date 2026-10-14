"""Opt-in MCP HTTP OAuth 2.1 bridge to the official Python SDK.

PKCE, PRM discovery, AS metadata, state/issuer checks and refresh are owned
by mcp.client.auth.OAuthClientProvider. Ridge only owns local callback
delivery and strict-permission token persistence. Never log token contents.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_GLOBAL_LOCK = threading.RLock()
_MAX_TOKEN_FILE = 64 * 1024


def validate_oauth_config(url: str, config: dict) -> dict:
    """Require an operator-selected HTTPS (or loopback HTTP) MCP resource."""
    if not isinstance(config, dict):
        raise ValueError("MCP OAuth configuration must be an object")
    uri = urlsplit(url)
    if uri.username or uri.password or uri.fragment or not uri.hostname:
        raise ValueError("MCP OAuth endpoint must not embed credentials")
    if uri.scheme != "https" and not (
        uri.scheme == "http" and uri.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("MCP OAuth endpoint must use HTTPS or loopback HTTP")
    port = config.get("callback_port", 8765)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("MCP OAuth callback_port must be 1024..65535")
    scope = config.get("scope", "")
    if not isinstance(scope, str) or len(scope) > 512 or "\n" in scope:
        raise ValueError("Invalid MCP OAuth scope")
    return {"callback_port": port, "scope": scope}


class FileOAuthTokenStorage:
    """Project-private 0600 JSON token storage with atomic updates.

    The SDK's own OAuthToken / OAuthClientInformationFull types are used on
    reads. The local machine's user account is the trust boundary; tokens
    are **not encrypted at rest**. Never use for shared/hosted deployments.
    """

    def __init__(self, project_root: str | Path, endpoint: str):
        self.root = Path(project_root).resolve()
        digest = hashlib.sha256(endpoint.encode()).hexdigest()[:32]
        self.directory = self.root / ".mycodeagent" / "oauth"
        self.path = self.directory / (digest + ".json")

    def _guard(self) -> None:
        cursor = self.root
        for part in self.path.relative_to(self.root).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("OAuth token storage must not use symbolic links")
        if not self.path.resolve().is_relative_to(self.root):
            raise ValueError("OAuth token storage escapes the project directory")

    def _read(self) -> dict:
        with _GLOBAL_LOCK:
            self._guard()
            try:
                if self.path.stat().st_size > _MAX_TOKEN_FILE:
                    raise ValueError("OAuth token storage exceeds size limit")
                result = json.loads(self.path.read_text(encoding="utf-8"))
                return result if isinstance(result, dict) else {}
            except FileNotFoundError:
                return {}

    def _write(self, field: str, value: dict) -> None:
        with _GLOBAL_LOCK:
            self._guard()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(self.directory, 0o700)
            data = self._read()
            data[field] = value
            tmp = None
            try:
                fd, name = tempfile.mkstemp(prefix=".oauth-", dir=self.directory)
                tmp = Path(name)
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
                tmp = None
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if isinstance(raw, dict) else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if isinstance(raw, dict) else None

    async def set_client_info(self, info) -> None:
        self._write("client_info", info.model_dump(mode="json"))


class LoopbackOAuthFlow:
    """One-shot local callback receiver; only used by explicit CLI login."""

    def __init__(self, port: int, *, open_browser: bool = True):
        self.port = port
        self.open_browser = open_browser
        self._server: asyncio.Server | None = None
        self._answer: asyncio.Future | None = None
        self._state: str | None = None

    async def redirect(self, url: str) -> None:
        from urllib.parse import parse_qs
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("OAuth authorization URL must use HTTPS")
        self._state = parse_qs(parsed.query).get("state", [None])[0]
        self._answer = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._serve_callback, "127.0.0.1", self.port)
        # Print only to an explicitly-invoked, interactive CLI. Never dump
        # authorization URLs in Agent traces, HTTP logs or Web responses.
        print("MCP OAuth: open the provider's authorization page in your browser.")
        if self.open_browser:
            webbrowser.open(url)
        else:
            print(url)

    async def _serve_callback(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > 4096:
                return
            parts = line.decode("ascii", errors="replace").split()
            if len(parts) != 3 or parts[0] != "GET":
                return
            uri = urlsplit(parts[1])
            if uri.path != "/callback":
                return
            params = parse_qs(uri.query)
            state = params.get("state", [None])[0]
            code = params.get("code", [None])[0]
            if not code or not state or state != self._state:
                return
            from mcp.client.auth import AuthorizationCodeResult
            response = AuthorizationCodeResult(
                code=code, state=state, iss=params.get("iss", [None])[0],
            )
            if self._answer is not None and not self._answer.done():
                self._answer.set_result(response)
            body = b"Authorization received. Return to Ridge Code."
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def callback(self):
        if self._answer is None:
            raise RuntimeError("MCP OAuth redirect not started")
        try:
            return await asyncio.wait_for(self._answer, timeout=180)
        finally:
            await self.close()

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def make_oauth_http_client(config, *, interactive: bool = False):
    """Return HTTP client plus optional loopback flow, no network side effects.

    OAuthClientProvider is part of the installed MCP SDK, which manages
    PKCE and token refresh. Interactive login is intentionally opt-in.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl
    opts = validate_oauth_config(config.url, config.oauth)
    flow = LoopbackOAuthFlow(opts["callback_port"]) if interactive else None

    async def redirect(url: str):
        if flow is None:
            raise RuntimeError(
                "MCP OAuth login required: run scripts/mcp_oauth_login.py locally"
            )
        await flow.redirect(url)

    async def callback():
        if flow is None:
            raise RuntimeError("MCP OAuth login required")
        return await flow.callback()

    provider = OAuthClientProvider(
        server_url=config.url,
        client_metadata=OAuthClientMetadata(
            client_name="Ridge Code",
            redirect_uris=[AnyUrl(f"http://127.0.0.1:{opts['callback_port']}/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=opts["scope"],
        ),
        storage=FileOAuthTokenStorage(config.project_root, config.url),
        redirect_handler=redirect,
        callback_handler=callback,
    )
    # SDK v2 uses httpx2, maintenance v1 uses httpx.
    import importlib.metadata
    major = int(importlib.metadata.version("mcp").split(".", 1)[0])
    if major >= 2:
        import httpx2 as httpx_backend
    else:
        import httpx as httpx_backend
    return httpx_backend.AsyncClient(auth=provider, timeout=30.0), flow
