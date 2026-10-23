"""ACP AskUser bridging over the non-standard session/request_input extension."""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

from ridgecode.acp import AcpServer


class FakeAgent:
    def __init__(self):
        self.event_sink = None
        self.ask_tool = SimpleNamespace(_input_func=None)
        self.tool_registry = SimpleNamespace(
            get_tool=lambda name: self.ask_tool if name == "AskUser" else None,
        )

    def run(self, text: str) -> str:
        return text


def _server(agent, **kwargs):
    out = io.StringIO()
    return AcpServer(lambda cwd: agent, stdout=out, **kwargs), out


def _messages(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _wait_for_input_request(out: io.StringIO, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for message in _messages(out):
            if message.get("method") == "session/request_input":
                return message
        time.sleep(0.01)
    raise AssertionError("no session/request_input was emitted")


def _new_session(server, request_id: int = 30) -> str:
    return server.handle({
        "jsonrpc": "2.0", "id": request_id, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]


def test_ask_user_is_bridged_to_the_editor():
    agent = FakeAgent()
    server, out = _server(agent)
    _new_session(server)
    assert callable(agent.ask_tool._input_func)

    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.update(answer=agent.ask_tool._input_func("Proceed?")),
    )
    thread.start()
    request = _wait_for_input_request(out)
    assert request["params"]["prompt"]["text"] == "Proceed?"

    server.handle({
        "jsonrpc": "2.0", "id": request["id"], "result": {"answer": "yes please"},
    })
    thread.join(timeout=2)
    assert holder["answer"] == "yes please"


def test_ask_user_falls_back_to_empty_on_client_error():
    agent = FakeAgent()
    server, out = _server(agent)
    _new_session(server)

    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.update(answer=agent.ask_tool._input_func("Ignore me")),
    )
    thread.start()
    request = _wait_for_input_request(out)
    server.handle({
        "jsonrpc": "2.0", "id": request["id"],
        "error": {"code": -32601, "message": "method not found"},
    })
    thread.join(timeout=2)
    assert holder["answer"] == ""


def test_ask_user_times_out_without_blocking_the_turn():
    agent = FakeAgent()
    out = io.StringIO()
    server = AcpServer(lambda cwd: agent, stdout=out, input_timeout_s=0.05)
    session_id = _new_session(server)

    started = time.monotonic()
    assert server._ask_user(session_id, "hello?") == ""
    assert time.monotonic() - started < 2
    assert not server._pending


def test_non_string_answer_is_treated_as_empty():
    agent = FakeAgent()
    server, out = _server(agent)
    session_id = _new_session(server)

    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.update(answer=server._ask_user(session_id, "pick")),
    )
    thread.start()
    request = _wait_for_input_request(out)
    server.handle({
        "jsonrpc": "2.0", "id": request["id"], "result": {"answer": {"unexpected": True}},
    })
    thread.join(timeout=2)
    assert holder["answer"] == ""
