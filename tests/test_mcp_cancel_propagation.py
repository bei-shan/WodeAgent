"""Agent cancellation propagates into MCP async calls and the wire envelope."""
import asyncio
import json
import threading
import time

import pytest

from core.runtime.cancellation import scoped_tool_cancellation
from tools.mcp.client import MCPClient, MCPClientConfig
from tools.mcp.adapter import MCPToolAdapter


def test_mcp_async_task_is_actually_cancelled_when_agent_interrupts():
    client = MCPClient(MCPClientConfig(transport="stdio", command="echo"))
    started = threading.Event()
    cancelled = threading.Event()
    interrupt = threading.Event()
    errors = []

    async def fake_call(_name, _arguments):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.call_tool = fake_call

    def worker():
        with scoped_tool_cancellation(interrupt):
            try:
                client.call_tool_sync("slow", {})
            except BaseException as exc:
                errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(timeout=3), "MCP coroutine never started"
    interrupt.set()
    thread.join(timeout=3)
    assert not thread.is_alive(), "MCP request kept blocking after interrupt"
    assert cancelled.is_set(), "Underlying asyncio task never received cancellation"
    assert len(errors) == 1 and isinstance(errors[0], InterruptedError)


def test_mcp_cancellation_before_dispatch_never_starts_remote():
    client = MCPClient(MCPClientConfig(transport="stdio", command="echo"))
    interrupt = threading.Event()
    interrupt.set()
    called = []
    async def fake_call(*_):
        called.append("called")
        return None
    client.call_tool = fake_call
    with scoped_tool_cancellation(interrupt):
        with pytest.raises(InterruptedError):
            client.call_tool_sync("never", {})
    assert called == []


def test_mcp_adapter_cancellation_response_uses_protocol():
    class FakeClient:
        def call_tool_sync(self, *_args):
            raise InterruptedError("remote confidential details")

    adapter = MCPToolAdapter(FakeClient(), "MCPTool", "remote", schema={"type": "object"})
    response = json.loads(adapter.run({}))
    assert response["status"] == "error"
    assert response["error"]["code"] == "INTERRUPTED"
    assert "confidential" not in response["error"]["message"]


def test_mcp_timeout_stays_distinct_from_cancellation():
    client = MCPClient(MCPClientConfig(transport="stdio", command="echo"))
    interrupt = threading.Event()
    async def fake_call(_name, _args):
        await asyncio.sleep(0)
        raise TimeoutError("transport deadline")
    client.call_tool = fake_call
    with scoped_tool_cancellation(interrupt):
        with pytest.raises(TimeoutError):
            client.call_tool_sync("timeout", {})


def test_mcp_two_agents_have_independent_events():
    client = MCPClient(MCPClientConfig(transport="stdio", command="echo"))
    interrupt_one = threading.Event()
    interrupt_two = threading.Event()
    started = threading.Barrier(2, timeout=3)
    states = []

    async def fake_call(name, args):
        if name == "fast":
            started.wait()
            await asyncio.sleep(0.2)
            return "ok"
        started.wait()
        await asyncio.sleep(10)
    client.call_tool = fake_call

    def run(name, event):
        with scoped_tool_cancellation(event):
            try:
                states.append((name, client.call_tool_sync(name, {})))
            except InterruptedError:
                states.append((name, "interrupted"))

    a = threading.Thread(target=run, args=("slow", interrupt_one))
    b = threading.Thread(target=run, args=("fast", interrupt_two))
    a.start()
    b.start()
    time.sleep(0.06)
    interrupt_one.set()
    a.join(timeout=3)
    b.join(timeout=3)
    assert not a.is_alive() and not b.is_alive()
    assert sorted(states) == [("fast", "ok"), ("slow", "interrupted")]
