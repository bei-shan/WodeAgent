"""Live localhost MCP Streamable HTTP integration; no fake SDK or network keys."""
from __future__ import annotations
import asyncio
import http.server
import json
import threading
from contextlib import contextmanager
from tools.mcp.client import MCPClient, MCPClientConfig

class MCPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: list[tuple[str, str]] = []
    lock = threading.Lock()
    count = 0

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
        if payload:
            self.wfile.write(payload)
        self.wfile.flush()

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
            with type(self).lock:
                type(self).count += 1
                count = type(self).count
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
