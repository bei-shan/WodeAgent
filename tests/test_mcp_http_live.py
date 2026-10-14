"""Live localhost MCP Streamable HTTP integration; no fake SDK or network keys."""
from __future__ import annotations
import asyncio
import http.server
import json
import socket
import threading
import time

import pytest

from core.runtime.cancellation import scoped_tool_cancellation
import tools.mcp.client as mcp_module
from contextlib import contextmanager
from tools.mcp.client import MCPClient, MCPClientConfig

class MCPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: list[tuple[str, str]] = []
    lock = threading.Lock()
    count = 0
    slow_started = threading.Event()

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict | None = None, *, init=False):
        payload = json.dumps(body).encode("utf8") if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "application/json")
        if init:
            self.send_header("Mcp-Session-Id", "ridge-test-session-1234")
        self.end_headers()
        try:
            if payload:
                self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path != "/mcp":
            return self._send(404, {})
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_000_000:
            return self._send(413, {"error": "too large"})
        message = json.loads(self.rfile.read(size))
        method = message.get("method")
        with type(self).lock:
            type(self).calls.append(("POST", str(method)))
        if "id" not in message:
            return self._send(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ridge-http-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "ping", "description": "HTTP MCP test",
                "inputSchema": {"type": "object", "properties": {
                    "value": {"type": "string"},
                }},
            }]}
        elif method == "tools/call":
            if message.get("params", {}).get("name") == "slow":
                type(self).slow_started.set()
                # Remote work can continue after client-side cancellation.
                time.sleep(0.6)
            with type(self).lock:
                type(self).count += 1
                count = type(self).count
            # Simulate a committed write whose response is lost while the
            # underlying TCP socket disappears. The client must not repeat.
            if message.get("params", {}).get("name") == "drop_after_commit":
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            value = str(message.get("params", {}).get("arguments", {}).get("value"))
            result = {
                "content": [{"type": "text", "text": f"http:{value}:{count}"}],
                "isError": False,
            }
        else:
            return self._send(200, {
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "No such MCP method"},
            })
        return self._send(200, {
            "jsonrpc": "2.0", "id": message["id"], "result": result,
        }, init=method == "initialize")

    def do_GET(self):
        # GET notification streams are optional for this smoke fixture.
        with type(self).lock:
            type(self).calls.append(("GET", self.path))
        return self._send(405)

    def do_DELETE(self):
        with type(self).lock:
            type(self).calls.append(("DELETE", self.path))
        return self._send(200)

@contextmanager
def live_server():
    MCPHandler.calls = []
    MCPHandler.count = 0
    MCPHandler.slow_started = threading.Event()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)

def _text(result) -> str:
    return "".join(str(x.text) for x in result.content if hasattr(x, "text"))

def test_real_streamable_http_sync_reuses_session_and_cleans_up():
    with live_server() as url:
        client = MCPClient(MCPClientConfig(transport="http", url=url))
        try:
            result = client.list_tools_sync()
            assert [tool.name for tool in result.tools] == ["ping"]
            first = _text(client.call_tool_sync("ping", {"value": "first"}))
            second = _text(client.call_tool_sync("ping", {"value": "second"}))
            assert first == "http:first:1"
            assert second == "http:second:2"
        finally:
            client.close_sync()
        assert not client.is_connected
        methods = [method for verb, method in MCPHandler.calls if verb == "POST"]
        assert methods.count("initialize") == 1
        assert methods.count("tools/list") == 1
        assert methods.count("tools/call") == 2

def test_real_streamable_http_async_session_and_clean_close():
    with live_server() as url:
        async def run():
            client = MCPClient(MCPClientConfig(transport="http", url=url))
            try:
                tools = await asyncio.wait_for(client.list_tools(), 10)
                assert [tool.name for tool in tools.tools] == ["ping"]
                assert _text(await asyncio.wait_for(
                    client.call_tool("ping", {"value": "async"}), 10
                )) == "http:async:1"
            finally:
                await asyncio.wait_for(client.close(), 10)
            assert not client.is_connected
        asyncio.run(run())
        assert sum(1 for verb, method in MCPHandler.calls
                   if verb == "POST" and method == "initialize") == 1


def test_real_http_tool_timeout_uses_local_deadline_not_server_completion(monkeypatch):
    monkeypatch.setattr(mcp_module, "_MCP_CALL_TIMEOUT", 0.12)
    with live_server() as url:
        c = MCPClient(MCPClientConfig(transport="http", url=url))
        try:
            c.list_tools_sync()
            before = time.monotonic()
            with pytest.raises(TimeoutError):
                c.call_tool_sync("slow", {"value": "delayed"})
            elapsed = time.monotonic() - before
            assert MCPHandler.slow_started.is_set()
            assert elapsed < 0.55, "MCP request ignored the configured deadline"
        finally:
            c.close_sync()
        assert not c.is_connected


def test_real_http_inflight_tool_receives_interrupt_and_releases_client():
    with live_server() as url:
        c = MCPClient(MCPClientConfig(transport="http", url=url))
        cancelled = threading.Event()
        errors: list[BaseException] = []
        try:
            c.list_tools_sync()
            def invoke():
                with scoped_tool_cancellation(cancelled):
                    try:
                        c.call_tool_sync("slow", {"value": "interrupt"})
                    except BaseException as exc:
                        errors.append(exc)
            worker = threading.Thread(target=invoke)
            worker.start()
            assert MCPHandler.slow_started.wait(timeout=4), "Remote tool was never invoked"
            before = time.monotonic()
            cancelled.set()
            worker.join(timeout=4)
            assert not worker.is_alive(), "Interrupt left MCP tool thread alive"
            assert len(errors) == 1 and isinstance(errors[0], InterruptedError), errors
            assert time.monotonic() - before < 0.55
        finally:
            c.close_sync()
        assert not c.is_connected


def test_lost_reply_does_not_reexecute_remote_side_effect_and_next_call_recovers(monkeypatch):
    # A remote tool may complete even when its HTTP response never arrives.
    # The SDK may surface a protocol error, EOF, or local timeout.
    monkeypatch.setattr(mcp_module, "_MCP_CALL_TIMEOUT", 0.35)
    with live_server() as url:
        c = MCPClient(MCPClientConfig(transport="http", url=url))
        try:
            c.list_tools_sync()
            with pytest.raises(Exception):
                c.call_tool_sync("drop_after_commit", {"value": "write-once"})
            committed = MCPHandler.count
            assert committed == 1, "Client automatically replayed a committed tool"
            # A new explicit user action can proceed after the failed call.
            result = _text(c.call_tool_sync("ping", {"value": "new-action"}))
            assert result == "http:new-action:2"
            assert MCPHandler.count == 2
            methods = [method for verb, method in MCPHandler.calls if verb == "POST"]
            assert methods.count("tools/call") == 2
        finally:
            c.close_sync()
