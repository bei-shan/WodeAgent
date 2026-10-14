"""MCP sync loop must terminate even when close races an active request."""
import asyncio
import threading
import time

import pytest

from tools.mcp.client import MCPClient, MCPClientConfig


def test_close_during_tool_call_stops_background_loop_after_last_user():
    client = MCPClient(MCPClientConfig(transport="stdio", command="echo"))
    started = threading.Event()
    release = threading.Event()
    result = []
    errors = []

    async def delayed(_name, _args):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "finished"

    client.call_tool = delayed

    def call():
        try:
            result.append(client.call_tool_sync("slow", {}))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    assert started.wait(3), "async tool never started"
    owner = client._sync_thread
    assert owner is not None and owner.is_alive()

    try:
        client.close_sync()
        assert owner.is_alive(), "loop stopped while an in-flight tool needed it"
        with pytest.raises(RuntimeError, match="closing"):
            client.list_tools_sync()
    finally:
        release.set()
        worker.join(3)
        owner.join(3)
    assert not worker.is_alive() and not owner.is_alive(), "MCP loop leaked after close"
    assert not errors
    assert result == ["finished"]
    assert client._sync_loop is None
    assert client._sync_pending == 0


def test_reopen_after_drained_close_creates_new_loop_without_ghost_threads():
    c = MCPClient(MCPClientConfig(transport="stdio", command="echo"))

    async def mock_tools():
        return "ok"

    c.list_tools = mock_tools
    try:
        for _ in range(3):
            assert c.list_tools_sync() == "ok"
            thread = c._sync_thread
            assert thread is not None
            c.close_sync()
            thread.join(3)
            assert not thread.is_alive()
            assert c._sync_loop is None
    finally:
        c.close_sync()
