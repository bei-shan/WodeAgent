"""MCP v2 dispatcher CLOSED errors must reconnect safely without tool replay."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tools.mcp.client import MCPClient, MCPClientConfig, _is_transport_disconnect


def _closed_error():
    try:
        from mcp.shared.exceptions import MCPError
        from mcp.types import CONNECTION_CLOSED
    except ImportError:
        pytest.skip("MCP v1 reports AnyIO transport exceptions")
    return MCPError(CONNECTION_CLOSED, "Connection closed")


def test_explicit_new_tool_call_recovers_after_v2_dispatcher_close():
    async def run():
        client = MCPClient(MCPClientConfig(transport="http", url="http://127.0.0.1:9999"))
        first = AsyncMock()
        first.call_tool.side_effect = _closed_error()
        second = AsyncMock()
        second.call_tool.return_value = "new action response"
        client.connect = AsyncMock(side_effect=[first, second])
        client.close = AsyncMock()
        with pytest.raises(ConnectionError, match="outcome unknown.*not replayed"):
            await client.call_tool("dangerous", {"action": "write"})
        assert first.call_tool.await_count == 1
        assert client.close.await_count == 1
        assert await client.call_tool("ping", {"value": "new"}) == "new action response"
        assert first.call_tool.await_count == 1  # no duplicate write
        second.call_tool.assert_awaited_once()
    asyncio.run(run())


def test_read_only_metadata_can_retry_once_on_v2_closed_dispatcher():
    async def run():
        client = MCPClient(MCPClientConfig(transport="http", url="http://127.0.0.1:9999"))
        first, second = AsyncMock(), AsyncMock()
        first.list_tools.side_effect = _closed_error()
        second.list_tools.return_value = {"tools": []}
        client.connect = AsyncMock(side_effect=[first, second])
        client.close = AsyncMock()
        assert await client.list_tools() == {"tools": []}
        first.list_tools.assert_awaited_once()
        second.list_tools.assert_awaited_once()
        assert client.close.await_count == 1
    asyncio.run(run())


def test_regular_mcp_protocol_error_is_not_misclassified_as_disconnect():
    async def run():
        from mcp.shared.exceptions import MCPError
        client = MCPClient(MCPClientConfig(transport="http", url="http://127.0.0.1:9999"))
        session = AsyncMock()
        invalid_params = MCPError(-32602, "Bad parameter")
        assert not _is_transport_disconnect(invalid_params)
        session.call_tool.side_effect = invalid_params
        client.connect = AsyncMock(return_value=session)
        client.close = AsyncMock()
        with pytest.raises(MCPError):
            await client.call_tool("bad", {})
        client.close.assert_not_awaited()
        session.call_tool.assert_awaited_once()
    try:
        from mcp.shared.exceptions import MCPError
    except ImportError:
        pytest.skip("MCP SDK v1 has different protocol error type")
    asyncio.run(run())


def test_mcp_timeout_retires_stale_transport_without_replaying_tool(monkeypatch):
    async def run():
        client = MCPClient(MCPClientConfig(transport="http", url="http://127.0.0.1:9999"))
        first, second = AsyncMock(), AsyncMock()

        async def never_finishes(*args, **kwargs):
            await asyncio.sleep(2)

        first.call_tool.side_effect = never_finishes
        second.call_tool.return_value = "fresh request"
        client.connect = AsyncMock(side_effect=[first, second])
        client.close = AsyncMock()
        from tools.mcp import client as module
        monkeypatch.setattr(module, "_MCP_CALL_TIMEOUT", 0.01)
        with pytest.raises(TimeoutError):
            await client.call_tool("write", {"key": "value"})
        first.call_tool.assert_awaited_once()
        assert client.close.await_count == 1
        assert await client.call_tool("ping", {}) == "fresh request"
        first.call_tool.assert_awaited_once()
        second.call_tool.assert_awaited_once()
    asyncio.run(run())
