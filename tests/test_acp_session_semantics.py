"""ACP session semantics: one prompt at a time, stale cancels ignored."""
from __future__ import annotations

import io
import json
import threading

from ridgecode.acp import AcpServer


class FakeAgent:
    def __init__(self):
        self.event_sink = None
        self.prompts: list[str] = []

    def run(self, text: str) -> str:
        self.prompts.append(text)
        return f"done:{text}"


class BlockingAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, text: str) -> str:
        self.prompts.append(text)
        self.started.set()
        self.release.wait(timeout=3)
        return f"done:{text}"

    def interrupt(self) -> None:
        self.release.set()


def _server(agent):
    out = io.StringIO()
    return AcpServer(lambda cwd: agent, stdout=out), out


def _messages(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _new_session(server, request_id: int = 40) -> str:
    return server.handle({
        "jsonrpc": "2.0", "id": request_id, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]


def _prompt(server, session_id: str, request_id: int, text: str = "go") -> dict | None:
    return server.handle({
        "jsonrpc": "2.0", "id": request_id, "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    })


def test_cancel_while_idle_does_not_poison_the_next_prompt():
    agent = FakeAgent()
    server, out = _server(agent)
    session_id = _new_session(server)

    server.handle({
        "jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id},
    })
    assert _prompt(server, session_id, 41) == {"stopReason": "end_turn"}
    assert agent.prompts == ["go"]


def test_a_second_concurrent_prompt_is_rejected_while_one_runs():
    agent = BlockingAgent()
    server, out = _server(agent)
    session_id = _new_session(server)

    first: dict = {}
    thread = threading.Thread(
        target=lambda: first.update(result=_prompt(server, session_id, 42)),
    )
    thread.start()
    assert agent.started.wait(timeout=2)

    second = _prompt(server, session_id, 43, "second")
    assert second is None
    error = _messages(out)[-1]["error"]
    assert error["code"] == -32600
    assert "running prompt" in error["message"]

    agent.release.set()
    thread.join(timeout=3)
    assert first["result"] == {"stopReason": "end_turn"}
    assert agent.prompts == ["go"]


def test_sessions_are_independent():
    agent = FakeAgent()
    server, out = _server(agent)
    first = _new_session(server, 44)
    second = _new_session(server, 45)

    assert _prompt(server, first, 46, "a") == {"stopReason": "end_turn"}
    assert _prompt(server, second, 47, "b") == {"stopReason": "end_turn"}
    assert agent.prompts == ["a", "b"]


def test_agent_failure_maps_to_internal_error():
    class Broken(FakeAgent):
        def run(self, text: str) -> str:
            raise RuntimeError("provider down")

    server, out = _server(Broken())
    session_id = _new_session(server, 48)

    assert _prompt(server, session_id, 49) is None
    error = _messages(out)[-1]["error"]
    assert error["code"] == -32603
    assert "agent failed" in error["message"]
