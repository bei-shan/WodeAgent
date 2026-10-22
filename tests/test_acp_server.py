"""ACP server: JSON-RPC framing, session lifecycle, streaming and cancel."""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

from core.events import AgentEvent, EventType
from ridgecode.acp import (
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    AcpServer,
)


class FakeAgent:
    def __init__(self, reply="hello from agent"):
        self.reply = reply
        self.event_sink = None
        self.prompts: list[str] = []
        self.interrupted = False

    def run(self, text: str) -> str:
        self.prompts.append(text)
        if self.event_sink is not None:
            self.event_sink.emit(AgentEvent(EventType.ASSISTANT_FINAL, {"content": self.reply}))
        return self.reply

    def interrupt(self) -> None:
        self.interrupted = True


class BlockingAgent:
    def __init__(self):
        self.event_sink = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.interrupted = False

    def run(self, text: str) -> str:
        self.started.set()
        self.release.wait(timeout=3)
        return "stopped"

    def interrupt(self) -> None:
        self.interrupted = True
        self.release.set()


def _server(agent, factory_calls=None):
    def factory(cwd):
        if factory_calls is not None:
            factory_calls.append(cwd)
        return agent

    out = io.StringIO()
    return AcpServer(factory, stdout=out), out


def _messages(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_initialize_negotiates_version_and_capabilities():
    server, out = _server(FakeAgent())
    result = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": 1, "clientCapabilities": {}},
    })
    assert result["protocolVersion"] == 1
    assert result["agentCapabilities"]["loadSession"] is True
    assert result["agentCapabilities"]["promptCapabilities"]["image"] is False
    assert _messages(out)[0]["id"] == 1


def test_session_new_uses_the_requested_workspace():
    calls: list[str] = []
    server, out = _server(FakeAgent(), factory_calls=calls)
    result = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "session/new",
        "params": {"cwd": "/tmp/workspace", "mcpServers": []},
    })
    assert result["sessionId"]
    assert calls == ["/tmp/workspace"]

    error = server.handle({"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {}})
    assert error is None
    assert _messages(out)[-1]["error"]["code"] == INVALID_PARAMS


def test_prompt_streams_agent_text_then_returns_end_turn():
    agent = FakeAgent("Final answer")
    server, out = _server(agent)
    session_id = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]

    result = server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "session/prompt",
        "params": {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "do the thing"}],
        },
    })
    assert result == {"stopReason": "end_turn"}
    assert agent.prompts == ["do the thing"]

    messages = _messages(out)
    update = next(m for m in messages if m.get("method") == "session/update")
    assert update["params"]["sessionId"] == session_id
    assert update["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert update["params"]["update"]["content"]["text"] == "Final answer"
    assert messages[-1]["id"] == 5


def test_prompt_joins_multiple_text_blocks_and_rejects_empty():
    agent = FakeAgent()
    server, out = _server(agent)
    session_id = server.handle({
        "jsonrpc": "2.0", "id": 6, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]

    server.handle({
        "jsonrpc": "2.0", "id": 7, "method": "session/prompt",
        "params": {
            "sessionId": session_id,
            "prompt": [
                {"type": "text", "text": "first"},
                {"type": "image", "data": "..."},
                {"type": "text", "text": "second"},
            ],
        },
    })
    assert agent.prompts == ["first\nsecond"]

    server.handle({
        "jsonrpc": "2.0", "id": 8, "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "image"}]},
    })
    assert _messages(out)[-1]["error"]["code"] == INVALID_PARAMS


def test_cancel_mid_prompt_returns_cancelled():
    agent = BlockingAgent()
    server, out = _server(agent)
    session_id = server.handle({
        "jsonrpc": "2.0", "id": 9, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]

    thread = threading.Thread(target=server.handle, args=({
        "jsonrpc": "2.0", "id": 10, "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "long job"}]},
    },))
    thread.start()
    assert agent.started.wait(timeout=2)

    server.handle({
        "jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id},
    })
    thread.join(timeout=3)
    assert agent.interrupted is True
    response = next(m for m in _messages(out) if m.get("id") == 10)
    assert response["result"] == {"stopReason": "cancelled"}


def test_session_load_and_unknown_methods():
    server, out = _server(FakeAgent())
    session_id = server.handle({
        "jsonrpc": "2.0", "id": 11, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]

    assert server.handle({
        "jsonrpc": "2.0", "id": 12, "method": "session/load",
        "params": {"sessionId": session_id, "cwd": "/tmp", "mcpServers": []},
    }) == {}

    server.handle({
        "jsonrpc": "2.0", "id": 13, "method": "session/load", "params": {"sessionId": "nope"},
    })
    assert _messages(out)[-1]["error"]["code"] == INVALID_PARAMS

    server.handle({"jsonrpc": "2.0", "id": 14, "method": "fs/read", "params": {}})
    assert _messages(out)[-1]["error"]["code"] == METHOD_NOT_FOUND


def test_serve_ignores_garbage_and_answers_valid_lines():
    agent = FakeAgent()
    lines = io.StringIO(
        'not json\n'
        '\n'
        '{"jsonrpc": "2.0", "id": 15, "method": "initialize", "params": {}}\n'
    )
    out = io.StringIO()
    server = AcpServer(lambda cwd: agent, stdin=lines, stdout=out)
    server.serve()

    messages = _messages(out)
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["result"]["protocolVersion"] == 1


def test_close_disposes_agents():
    closed = []

    class Agent(FakeAgent):
        def close(self):
            closed.append(True)

    server, _ = _server(Agent())
    server.handle({"jsonrpc": "2.0", "id": 16, "method": "session/new", "params": {"cwd": "/tmp"}})
    server.close()
    assert closed == [True]


# ---------------------------------------------------------------------------
# Permission approvals
# ---------------------------------------------------------------------------
def _wait_for_permission_request(out: io.StringIO, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for message in _messages(out):
            if message.get("method") == "session/request_permission":
                return message
        time.sleep(0.01)
    raise AssertionError("no session/request_permission was emitted")


def _ask_in_thread(server, session_id, holder):
    holder["decision"] = server._ask_permission(session_id, "/tmp/outside", "Write", "write")
    holder["done"].set()


def _new_session(server, request_id: int = 20) -> str:
    return server.handle({
        "jsonrpc": "2.0", "id": request_id, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]


def test_permission_request_is_forwarded_and_granted():
    server, out = _server(FakeAgent())
    session_id = _new_session(server)
    holder: dict = {"done": threading.Event()}
    thread = threading.Thread(target=_ask_in_thread, args=(server, session_id, holder))
    thread.start()

    request = _wait_for_permission_request(out)
    assert request["params"]["sessionId"] == session_id
    assert request["params"]["toolCall"]["title"] == "Write: write"
    assert [option["optionId"] for option in request["params"]["options"]] == ["allow", "reject"]

    server.handle({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"outcome": {"outcome": "selected", "optionId": "allow"}},
    })
    assert holder["done"].wait(timeout=2)
    thread.join(timeout=2)
    assert holder["decision"] == "granted"


def test_permission_rejection_and_cancellation_deny():
    for outcome in ({"outcome": "selected", "optionId": "reject"}, {"outcome": "cancelled"}):
        server, out = _server(FakeAgent())
        session_id = _new_session(server)
        holder: dict = {"done": threading.Event()}
        thread = threading.Thread(target=_ask_in_thread, args=(server, session_id, holder))
        thread.start()

        request = _wait_for_permission_request(out)
        server.handle({"jsonrpc": "2.0", "id": request["id"], "result": {"outcome": outcome}})
        assert holder["done"].wait(timeout=2)
        thread.join(timeout=2)
        assert holder["decision"] == "denied"


def test_permission_timeout_denies():
    agent = FakeAgent()
    out = io.StringIO()
    server = AcpServer(lambda cwd: agent, stdout=out, permission_timeout_s=0.05)
    session_id = _new_session(server)

    started = time.monotonic()
    assert server._ask_permission(session_id, "/tmp/x", "Write", "write") == "denied"
    assert time.monotonic() - started < 2
    # The stale request id must not leak into the pending table.
    assert not server._pending


def test_brokers_are_installed_on_the_agent():
    class Agent(FakeAgent):
        def __init__(self):
            super().__init__()
            self._permission_gate = SimpleNamespace(_broker=None)
            self.bash_tool = SimpleNamespace(_command_approval_broker=None)
            self.tool_registry = SimpleNamespace(
                get_tool=lambda name: self.bash_tool if name == "Bash" else None,
            )

    agent = Agent()
    server, out = _server(agent)
    _new_session(server)

    assert callable(agent._permission_gate._broker)
    assert callable(agent.bash_tool._command_approval_broker)

    holder: dict = {"done": threading.Event()}
    thread = threading.Thread(
        target=lambda: holder.update(decision=agent._permission_gate._broker("cmd", "Bash", "execute-command")),
    )
    thread.start()
    request = _wait_for_permission_request(out)
    assert request["params"]["toolCall"]["title"] == "Bash: execute-command"
    server.handle({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"outcome": {"outcome": "selected", "optionId": "allow"}},
    })
    thread.join(timeout=2)
    assert holder["decision"] == "granted"
