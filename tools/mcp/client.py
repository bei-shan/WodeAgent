"""MCP client wrapper for stdio/HTTP transports with thread-safe connection management."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
import anyio

logger = logging.getLogger(__name__)

# Per-call timeout for regular MCP tool invocations (seconds).
_MCP_CALL_TIMEOUT = float(os.environ.get("MCP_CALL_TIMEOUT", "30"))
# Longer timeout for list_tools (npx downloads can take tens of seconds).
_MCP_LIST_TOOLS_TIMEOUT = float(os.environ.get("MCP_LIST_TOOLS_TIMEOUT", "60"))
# Timeout for the initial connect + session initialization.
_MCP_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "30"))


@dataclass
class MCPClientConfig:
    transport: str
    command: Optional[str] = None
    args: Optional[list[str]] = None
    url: Optional[str] = None
    env: Optional[dict[str, str]] = None


class MCPClient:
    """Async MCP client with lazy connection management and thread safety.

    Thread safety: all state-mutating operations are protected by ``self._lock``.
    The fast-path read of ``self._session`` (to skip connection when already
    connected) is intentionally lock-free — the worst case is two threads both
    enter ``connect()`` and one waits at the lock for the other to finish, then
    returns the already-established session via double-check.

    Timeouts: every MCP operation is wrapped with ``asyncio.wait_for``.
    The per-call timeout defaults to 30 s (``MCP_CALL_TIMEOUT``) for regular
    calls and 60 s (``MCP_LIST_TOOLS_TIMEOUT``) for ``list_tools``.
    """

    def __init__(self, config: MCPClientConfig):
        self._config = config
        self._conn = None
        self._session: Optional[ClientSession] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> ClientSession:
        """Establish (or return existing) connection. Thread-safe via lock."""
        # Fast path: already connected — no lock needed.
        if self._session is not None:
            return self._session

        with self._lock:
            # Double-check: another thread may have connected while we waited.
            if self._session is not None:
                return self._session

            if self._config.transport == "stdio":
                env = None
                if self._config.env is not None:
                    env = dict(os.environ)
                    env.update(self._config.env)
                params = StdioServerParameters(
                    command=self._config.command,
                    args=self._config.args or [],
                    env=env,
                )
                self._conn = stdio_client(params)
            else:
                if not self._config.url:
                    raise ValueError("MCPClientConfig.url is required for http transport")
                self._conn = streamablehttp_client(self._config.url)

            try:
                read, write, *_ = await asyncio.wait_for(
                    self._conn.__aenter__(),
                    timeout=_MCP_CONNECT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._conn = None
                raise TimeoutError(
                    f"MCP transport open timed out after {_MCP_CONNECT_TIMEOUT:.0f}s"
                )
            except Exception:
                self._conn = None
                raise

            session = ClientSession(read, write)
            try:
                await session.__aenter__()
                try:
                    await asyncio.wait_for(
                        session.initialize(),
                        timeout=_MCP_CONNECT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"MCP session initialize timed out after {_MCP_CONNECT_TIMEOUT:.0f}s"
                    )
            except Exception:
                try:
                    await session.__aexit__(None, None, None)
                finally:
                    self._session = None
                try:
                    await self._conn.__aexit__(None, None, None)
                finally:
                    self._conn = None
                raise

            self._session = session
            return self._session

    async def close(self) -> None:
        """Close session and transport. Thread-safe via lock."""
        with self._lock:
            if self._session:
                await self._session.__aexit__(None, None, None)
                self._session = None
            if self._conn:
                await self._conn.__aexit__(None, None, None)
                self._conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_with_retry(self, operation_name: str, coro_factory, timeout: float) -> Any:
        """Execute *coro_factory(session)* with a single retry on transport
        error, wrapped in ``asyncio.wait_for``.

        Raises:
            TimeoutError: if the operation exceeds *timeout* seconds.
            ConnectionError: if the transport is closed and the retry also fails.
        """
        async def _do_call(session):
            try:
                return await asyncio.wait_for(coro_factory(session), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"MCP operation '{operation_name}' timed out after {timeout:.0f}s"
                )

        session = await self.connect()
        try:
            return await _do_call(session)
        except TimeoutError:
            raise
        except anyio.ClosedResourceError:
            logger.debug("MCP transport closed during '%s', reconnecting...", operation_name)
            await self.close()
            session = await self.connect()
            try:
                return await _do_call(session)
            except anyio.ClosedResourceError:
                raise ConnectionError(
                    f"MCP transport closed during '{operation_name}' and reconnection failed"
                )

    # ------------------------------------------------------------------
    # MCP operations
    # ------------------------------------------------------------------

    async def list_tools(self) -> Any:
        return await self._call_with_retry(
            "list_tools",
            lambda s: s.list_tools(),
            timeout=_MCP_LIST_TOOLS_TIMEOUT,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._call_with_retry(
            f"call_tool:{name}",
            lambda s: s.call_tool(name, arguments),
            timeout=_MCP_CALL_TIMEOUT,
        )

    async def list_resources(self) -> Any:
        return await self._call_with_retry(
            "list_resources",
            lambda s: s.list_resources(),
            timeout=_MCP_CALL_TIMEOUT,
        )

    async def read_resource(self, uri: Any) -> Any:
        return await self._call_with_retry(
            "read_resource",
            lambda s: s.read_resource(uri),
            timeout=_MCP_CALL_TIMEOUT,
        )

    async def list_prompts(self) -> Any:
        return await self._call_with_retry(
            "list_prompts",
            lambda s: s.list_prompts(),
            timeout=_MCP_CALL_TIMEOUT,
        )

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._call_with_retry(
            f"get_prompt:{name}",
            lambda s: s.get_prompt(name, arguments=arguments),
            timeout=_MCP_CALL_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Sync wrappers (thread-safe)
    # ------------------------------------------------------------------

    def _run_sync(self, coro):
        """Run a coroutine synchronously in a fresh event loop.

        Each call creates its own event loop so that sync methods from
        different threads don't contend for the same asyncio loop (which
        would fail with "event loop is already running").  The lock in
        ``connect()`` / ``close()`` serializes actual state mutations;
        the loop itself is thread-local.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        raise RuntimeError(
            "MCPClient sync methods cannot run inside an active event loop."
        )

    def connect_sync(self) -> ClientSession:
        return self._run_sync(self.connect())

    def call_tool_sync(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._run_sync(self.call_tool(name, arguments))

    def list_tools_sync(self) -> Any:
        return self._run_sync(self.list_tools())

    def close_sync(self) -> None:
        return self._run_sync(self.close())

    # ------------------------------------------------------------------
    # Introspection (lock-free reads)
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return True if a session is currently established (best-effort)."""
        return self._session is not None

    @property
    def config(self) -> MCPClientConfig:
        return self._config
