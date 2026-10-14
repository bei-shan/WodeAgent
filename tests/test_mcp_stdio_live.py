"""Live MCP stdio transport (not mocked) and persistent sync-loop lifecycle."""
import asyncio
import sys
from pathlib import Path

from tools.mcp.client import MCPClient, MCPClientConfig


SERVER = Path(__file__).parent / "fixtures" / "mcp_stdio_echo_server.py"


def client():
    return MCPClient(MCPClientConfig(
        transport="stdio", command=sys.executable, args=["-u", str(SERVER)],
    ))


def _text(result):
    blocks = result.content
    return "\n".join(getattr(block, "text", "") for block in blocks)


def test_real_stdio_async_lifecycle_connect_reuse_tools_and_close():
    async def run():
        c = client()
        try:
            first = await asyncio.wait_for(c.connect(), 10)
            second = await asyncio.wait_for(c.connect(), 10)
            assert first is second
            tools = await asyncio.wait_for(c.list_tools(), 10)
            assert [tool.name for tool in tools.tools] == ["ping"]
            a = _text(await asyncio.wait_for(c.call_tool("ping", {"value": "one"}), 10))
            b = _text(await asyncio.wait_for(c.call_tool("ping", {"value": "two"}), 10))
            assert "ping:one:count:1:pid:" in a
            assert "ping:two:count:2:pid:" in b
            assert a.split(":pid:")[1] == b.split(":pid:")[1]
        finally:
            await asyncio.wait_for(c.close(), 10)
        assert not c.is_connected
    asyncio.run(run())


def test_real_stdio_sync_calls_reuse_one_live_connection_and_cleanup():
    c = client()
    try:
        tools = c.list_tools_sync()
        assert [tool.name for tool in tools.tools] == ["ping"]
        a = _text(c.call_tool_sync("ping", {"value": "one"}))
        b = _text(c.call_tool_sync("ping", {"value": "two"}))
        assert "ping:one:count:1:pid:" in a
        assert "ping:two:count:2:pid:" in b
        assert a.split(":pid:")[1] == b.split(":pid:")[1]
    finally:
        c.close_sync()
    assert not c.is_connected
