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
# Both SDK majors retain ClientSession and stdio. Streamable HTTP's
# deprecated spelling was removed in v2, while its two-stream return
# contract works with our read/write-only consumer.
from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
import anyio
from core.runtime.cancellation import current_cancel_event

logger = logging.getLogger(__name__)


def _is_transport_disconnect(exc: Exception) -> bool:
    """Recognize transport closure in both MCP Python SDK major versions.

    SDK v2 converts dispatcher shutdown to MCPError(CONNECTION_CLOSED)
    instead of raising an AnyIO ClosedResourceError. Ordinary remote
    JSON-RPC errors must never be treated as disconnected transports.
    """
    if isinstance(exc, (
        anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream,
    )):
        return True
    try:
        from mcp.shared.exceptions import MCPError
        from mcp.types import CONNECTION_CLOSED
    except ImportError:
        return False  # v1 SDK uses the AnyIO exceptions above
    return isinstance(exc, MCPError) and getattr(exc, "code", None) == CONNECTION_CLOSED


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
    oauth: Optional[dict[str, Any]] = None
    project_root: Optional[str] = None
    oauth_interactive: bool = False


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
        self._session_loop_id: int | None = None  # detect stale sessions
        self._lock = threading.Lock()  # compatibility for external inspection
        self._connection_guard = asyncio.Lock()
        self._owner_task: asyncio.Task | None = None
        self._owner_ready: asyncio.Future | None = None
        self._owner_shutdown: asyncio.Event | None = None
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._sync_thread: threading.Thread | None = None
        self._sync_guard = threading.Lock()
        self._sync_pending = 0
        self._sync_stop_when_idle = False

    # ------------------------------------------------------------------
    # Connection lifecycle — one owner task per live transport
    # ------------------------------------------------------------------

    async def _own_connection(self, ready: asyncio.Future, shutdown: asyncio.Event) -> None:
        """Enter and exit SDK task groups in the SAME asyncio task.

        AnyIO transport cancel scopes must not be entered from a different
        task than the one performing __aexit__. Calls from other tasks use
        the initialized session but never own its context managers.
        """
        connection = None
        session = None
        entered_transport = False
        entered_session = False
        oauth_http_client = None
        oauth_flow = None
        try:
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
                connection = stdio_client(params)
            else:
                if not self._config.url:
                    raise ValueError("MCPClientConfig.url is required for http transport")
                if self._config.oauth is not None:
                    from tools.mcp.oauth import make_oauth_http_client
                    oauth_http_client, oauth_flow = make_oauth_http_client(
                        self._config, interactive=self._config.oauth_interactive
                    )
                    await oauth_http_client.__aenter__()
                    connection = streamablehttp_client(
                        self._config.url, http_client=oauth_http_client
                    )
                else:
                    connection = streamablehttp_client(self._config.url)
            self._conn = connection
            # asyncio.timeout is scoped to this task; asyncio.wait_for on
            # __aenter__ would create a child task and violate AnyIO ownership.
            connect_timeout = 240 if self._config.oauth_interactive else _MCP_CONNECT_TIMEOUT
            async with asyncio.timeout(connect_timeout):
                streams = await connection.__aenter__()
            entered_transport = True
            read, write, *_ = streams
            session = ClientSession(read, write)
            await session.__aenter__()
            entered_session = True
            try:
                await asyncio.wait_for(session.initialize(), timeout=_MCP_CONNECT_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"MCP session initialize timed out after {_MCP_CONNECT_TIMEOUT:.0f}s"
                ) from exc
            self._session = session
            self._session_loop_id = id(asyncio.get_running_loop())
            if not ready.done():
                ready.set_result(session)
            await shutdown.wait()
        except BaseException as exc:
            if not ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    ready.cancel()
                elif isinstance(exc, TimeoutError) and not isinstance(exc, asyncio.TimeoutError):
                    ready.set_exception(exc)
                elif isinstance(exc, asyncio.TimeoutError):
                    ready.set_exception(TimeoutError(
                        f"MCP transport open timed out after {_MCP_CONNECT_TIMEOUT:.0f}s"
                    ))
                else:
                    ready.set_exception(exc)
            else:
                logger.debug("MCP transport closed", exc_info=True)
        finally:
            # The cleanup always runs in the task that entered both contexts.
            try:
                if entered_session:
                    await session.__aexit__(None, None, None)
            except BaseException:
                logger.debug("MCP session close failed", exc_info=True)
            try:
                if entered_transport:
                    await connection.__aexit__(None, None, None)
            except BaseException:
                logger.debug("MCP transport close failed", exc_info=True)
            if oauth_flow is not None:
                try:
                    await oauth_flow.close()
                except Exception:
                    logger.debug("OAuth callback cleanup failed", exc_info=True)
            if oauth_http_client is not None:
                try:
                    await oauth_http_client.__aexit__(None, None, None)
                except Exception:
                    logger.debug("OAuth HTTP client cleanup failed", exc_info=True)
            self._session = None
            self._session_loop_id = None
            self._conn = None

    async def connect(self) -> ClientSession:
        """Open the connection once for this loop and safely share requests."""
        loop_id = id(asyncio.get_running_loop())
        if self._session is not None and self._session_loop_id != loop_id:
            raise RuntimeError("MCP session belongs to another event loop; close it there first")
        if self._session is not None:
            return self._session
        async with self._connection_guard:
            if self._session is not None:
                return self._session
            if self._owner_task is not None and self._owner_task.done():
                self._owner_task = None
            if self._owner_task is None:
                ready = asyncio.get_running_loop().create_future()
                shutdown = asyncio.Event()
                self._owner_ready = ready
                self._owner_shutdown = shutdown
                self._owner_task = asyncio.create_task(
                    self._own_connection(ready, shutdown), name="mcp-transport-owner"
                )
            try:
                timeout = 250 if self._config.oauth_interactive else _MCP_CONNECT_TIMEOUT * 2
                return await asyncio.wait_for(
                    asyncio.shield(self._owner_ready), timeout=timeout
                )
            except BaseException:
                if self._owner_shutdown is not None:
                    self._owner_shutdown.set()
                if self._owner_task is not None:
                    await asyncio.gather(self._owner_task, return_exceptions=True)
                self._owner_task = None
                raise

    async def close(self) -> None:
        """Close this transport on its owner loop, never on a foreign loop."""
        if self._session_loop_id is not None and (
            self._session_loop_id != id(asyncio.get_running_loop())
        ):
            raise RuntimeError("MCP transport must be closed on the owning loop")
        async with self._connection_guard:
            owner = self._owner_task
            if owner is None:
                return
            if self._owner_shutdown is not None:
                self._owner_shutdown.set()
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=5.0)
            except asyncio.TimeoutError:
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
            finally:
                self._owner_task = None
                self._owner_shutdown = None
                self._owner_ready = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self, operation_name: str, coro_factory, timeout: float,
        *, retry_on_disconnect: bool = True,
    ) -> Any:
        """Retry disconnects only on read-only operations, never tools.

        A write-capable MCP tool might complete remotely before its reply
        is lost. Replaying it automatically would duplicate side effects.
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
            # A timeout does not tell us whether the remote mutation ran.
            # Invalidate the potentially half-closed dispatcher so a new,
            # explicitly requested tool call can use a fresh connection.
            # Never replay the timed-out call.
            try:
                await self.close()
            except Exception:
                logger.warning("Unable to retire timed-out MCP transport", exc_info=True)
            raise
        except Exception as exc:
            if not _is_transport_disconnect(exc):
                raise
            logger.warning("MCP transport dropped during '%s'; invalidating session", operation_name)
            await self.close()
            if not retry_on_disconnect:
                raise ConnectionError(
                    f"MCP tool '{operation_name}' outcome unknown after transport loss; "
                    "request was not replayed. Check remote state before retrying."
                ) from exc
            session = await self.connect()
            try:
                return await _do_call(session)
            except Exception as retry_exc:
                if not _is_transport_disconnect(retry_exc):
                    raise
                await self.close()
                raise ConnectionError(
                    f"MCP transport closed during '{operation_name}' and reconnection failed"
                ) from retry_exc

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
            retry_on_disconnect=False,
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
        """Execute on this client's persistent background event loop.

        SDK transports own AnyIO task groups: a loop that disappears after
        every call destroys the server subprocess and loses session state.
        Different caller threads submit work to the SAME live loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise RuntimeError("MCPClient sync methods cannot run inside an active event loop.")

        with self._sync_guard:
            if self._sync_stop_when_idle:
                coro.close()
                raise RuntimeError("MCP client is closing; wait for pending tool calls")
            if self._sync_loop is None or not self._sync_loop.is_running():
                loop = asyncio.new_event_loop()
                started = threading.Event()

                def serve():
                    asyncio.set_event_loop(loop)
                    loop.call_soon(started.set)
                    try:
                        loop.run_forever()
                    finally:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        loop.close()
                        asyncio.set_event_loop(None)

                thread = threading.Thread(
                    target=serve, daemon=True, name="mcp-session-loop"
                )
                thread.start()
                if not started.wait(timeout=5):
                    coro.close()
                    raise RuntimeError("MCP session event loop did not start")
                self._sync_loop = loop
                self._sync_thread = thread
            self._sync_pending += 1
            future = asyncio.run_coroutine_threadsafe(coro, self._sync_loop)
        try:
            return future.result()
        finally:
            loop_to_stop = None
            with self._sync_guard:
                self._sync_pending -= 1
                if self._sync_pending == 0 and self._sync_stop_when_idle:
                    loop_to_stop = self._sync_loop
                    self._sync_loop = None
                    self._sync_thread = None
                    self._sync_stop_when_idle = False
            if loop_to_stop is not None:
                loop_to_stop.call_soon_threadsafe(loop_to_stop.stop)

    def connect_sync(self) -> ClientSession:
        return self._run_sync(self.connect())

    async def _call_tool_cancellable(
        self, name: str, arguments: dict[str, Any], cancel_event: threading.Event,
    ) -> Any:
        """Cancel the in-flight MCP coroutine when this Agent turn is stopped.

        Cancelling a client request is not a guarantee the remote server has
        rolled back side effects already started. Timeout still belongs to
        the underlying _call_with_retry implementation.
        """
        if cancel_event.is_set():
            raise InterruptedError("MCP request cancelled")
        task = asyncio.create_task(self.call_tool(name, arguments))
        try:
            while not task.done():
                if cancel_event.is_set():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=2.0)
                    except BaseException:
                        pass
                    raise InterruptedError("MCP request cancelled")
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                task.cancel()

    def call_tool_sync(self, name: str, arguments: dict[str, Any]) -> Any:
        cancellation = current_cancel_event()
        if cancellation is None:
            return self._run_sync(self.call_tool(name, arguments))
        return self._run_sync(self._call_tool_cancellable(name, arguments, cancellation))

    def list_tools_sync(self) -> Any:
        return self._run_sync(self.list_tools())

    def close_sync(self) -> None:
        """Shutdown transport and stop the loop after ALL in-flight calls drain.

        Previously close_sync() left an idle daemon loop alive if another
        thread was still completing a tool call. The last caller now owns
        stopping the loop; no new calls are admitted while it is draining.
        """
        with self._sync_guard:
            loop = self._sync_loop
        if loop is None:
            return
        try:
            self._run_sync(self.close())
        finally:
            with self._sync_guard:
                if self._sync_loop is loop:
                    self._sync_stop_when_idle = True
                    if self._sync_pending == 0:
                        self._sync_loop = None
                        thread = self._sync_thread
                        self._sync_thread = None
                        self._sync_stop_when_idle = False
                        loop.call_soon_threadsafe(loop.stop)
                    else:
                        thread = None  # last in-flight caller stops the loop
                else:
                    thread = None
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)

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
