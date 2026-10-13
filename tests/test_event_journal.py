"""Tests for replayable bounded per-session event delivery."""

import queue
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.events import AgentEvent
from core.runtime.event_journal import EventJournal
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def event(name: str) -> AgentEvent:
    return AgentEvent(name, {"content": name}, step=1)


def test_journal_sequences_and_independent_subscribers():
    journal = EventJournal(capacity=5)
    assert journal.latest_seq == 0
    assert journal.put(event("first")) == 1
    assert journal.put(event("second")) == 2
    first = journal.read_after(0)
    another = journal.read_after(0)
    assert [x.seq for x in first.events] == [1, 2]
    assert [x.seq for x in another.events] == [1, 2]
    assert journal.read_after(2).events == ()
    assert journal.get_nowait().type == "first"
    assert journal.get_nowait().type == "second"
    with pytest.raises(queue.Empty):
        journal.get_nowait()


def test_journal_bounded_gap_and_cursor_advance():
    journal = EventJournal(capacity=2)
    for idx in range(5):
        journal.put(event(str(idx)))
    batch = journal.read_after(0)
    assert batch.gap is True
    assert batch.oldest_seq == 4
    assert batch.latest_seq == 5
    assert [x.seq for x in batch.events] == [4, 5]
    assert journal.read_after(4).gap is False
    assert [x.seq for x in journal.read_after(4).events] == [5]
    with pytest.raises(ValueError):
        journal.read_after(-1)


def test_wait_after_does_not_miss_concurrent_publisher():
    journal = EventJournal()
    result = []

    def reader():
        result.append(journal.wait_after(0, timeout=2))

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.02)
    journal.put(event("ready"))
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result[0].events[0].event.type == "ready"


def test_websocket_replay_and_two_concurrent_readers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        session = app.state.controller.get_session(sid)
        session.events.put(event("step.started"))
        session.events.put(event("llm.started"))
        with client.websocket_connect(f"/api/sessions/{sid}/stream?after=0") as first:
            with client.websocket_connect(f"/api/sessions/{sid}/stream?after=0") as second:
                # Each subscriber sees the same history instead of competing
                # over a queue. Sequence numbers are stable across connections.
                assert [first.receive_json()["seq"] for _ in range(2)] == [1, 2]
                assert [second.receive_json()["seq"] for _ in range(2)] == [1, 2]
                session.events.put(event("tool.started"))
                a = first.receive_json()
                b = second.receive_json()
                assert a["seq"] == b["seq"] == 3
                assert a["type"] == b["type"] == "tool.started"

        session.events.put(event("tool.completed"))
        with client.websocket_connect(f"/api/sessions/{sid}/stream?after=3") as reconnect:
            recovered = reconnect.receive_json()
            assert recovered["seq"] == 4
            assert recovered["type"] == "tool.completed"


def test_websocket_expired_cursor_signals_reset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        session = app.state.controller.get_session(sid)
        session.events = EventJournal(capacity=2)
        for idx in range(5):
            session.events.put(event("step.started"))
        with client.websocket_connect(f"/api/sessions/{sid}/stream?after=1") as ws:
            reset = ws.receive_json()
            assert reset["type"] == "stream.reset"
            assert reset["payload"]["reason"] == "cursor_expired"
            assert reset["payload"]["oldest_seq"] == 4
            assert [ws.receive_json()["seq"] for _ in range(2)] == [4, 5]


def test_new_client_recovers_pending_permission(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        session = app.state.controller.get_session(sid)
        prompt = AgentEvent("permission.requested", {
            "request_id": "req1", "tool": "Write",
            "path": "example.py", "action": "write",
        })
        with session._perm_lock:
            session._pending_permissions["req1"] = (threading.Event(), {})
            session._pending_permission_events["req1"] = prompt

        with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
            frame = ws.receive_json()
            assert frame["type"] == "permission.requested"
            assert frame["payload"]["request_id"] == "req1"
            assert "seq" not in frame

        assert session.resolve_permission("req1", "granted")
        assert session.pending_prompts() == []

def test_reconnecting_with_stale_future_cursor_after_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        session = app.state.controller.get_session(sid)
        with client.websocket_connect(f"/api/sessions/{sid}/stream?after=123") as ws:
            reset = ws.receive_json()
            assert reset["type"] == "stream.reset"
            assert reset["payload"]["reason"] == "cursor_ahead"
            assert reset["payload"]["resume_from"] == 0
            session.events.put(event("step.started"))
            next_frame = ws.receive_json()
            assert next_frame["seq"] == 1
