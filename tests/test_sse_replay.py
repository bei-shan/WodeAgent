"""Deterministic SSE replay, reconnect, epoch resets and auth gates."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from core.events import AgentEvent
from core.runtime.event_journal import EventJournal
from desktop.service.app import create_app
from desktop.service.sse_stream import parse_event_cursor, follow_events
from tests.test_web_service import FakeAgent


def event(kind: str, name: str) -> AgentEvent:
    return AgentEvent(kind, {"content": name}, step=2)


def unpack(chunk: bytes) -> tuple[str | None, dict | None]:
    lines = chunk.decode().splitlines()
    event_id = next((line[4:].strip() for line in lines if line.startswith("id:")), None)
    data = next((line[6:] for line in lines if line.startswith("data:")), None)
    return event_id, (json.loads(data) if data else None)


def test_epoch_cursor_and_process_restart_are_unambiguous():
    one = EventJournal()
    assert parse_event_cursor(None, one, busy=False) == (0, None)
    one.put(event("step.started", "one"))
    assert parse_event_cursor(f"{one.stream_id}:0", one, busy=False) == (0, None)
    assert parse_event_cursor("1", one, busy=False) == (1, None)
    assert parse_event_cursor("222", one, busy=False) == (1, "cursor_ahead")
    two = EventJournal()
    assert one.stream_id != two.stream_id
    assert parse_event_cursor(f"{one.stream_id}:1", two, busy=False) == (0, "stream_restarted")
    for invalid in ("-1", "1.0", "garbage", "12345678901234567890", "bad:1", "1\n2"):
        with pytest.raises(ValueError):
            parse_event_cursor(invalid, one, busy=False)


def test_sse_replays_after_cursor_and_handles_ring_gap():
    async def check():
        journal = EventJournal(capacity=2)
        for n in range(5):
            journal.put(event("step.started", str(n)))
        async def not_closed():
            return False
        stream = follow_events(
            journal, cursor=1, reset_reason=None,
            events={"step.started"}, sanitize=lambda payload: payload,
            disconnected=not_closed,
        )
        first = unpack(await anext(stream))
        assert first[1]["type"] == "stream.reset"
        assert first[1]["payload"]["reason"] == "cursor_expired"
        assert first[1]["payload"]["resume_from"] == 3
        assert first[0] == f"{journal.stream_id}:3"
        a = unpack(await anext(stream))
        b = unpack(await anext(stream))
        assert [a[1]["seq"], b[1]["seq"]] == [4, 5]
        assert [a[0], b[0]] == [f"{journal.stream_id}:4", f"{journal.stream_id}:5"]
        await stream.aclose()
    asyncio.run(check())


def test_sse_waits_for_concurrent_event_without_losing_it():
    async def check():
        journal = EventJournal(capacity=5)
        async def disconnected():
            return False
        stream = follow_events(
            journal, cursor=0, reset_reason=None,
            events={"step.started"}, sanitize=lambda p: p,
            disconnected=disconnected,
        )
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.04)
        journal.put(event("step.started", "live"))
        eid, msg = unpack(await asyncio.wait_for(task, timeout=2))
        assert msg["payload"]["content"] == "live"
        assert eid == f"{journal.stream_id}:1"
        await stream.aclose()
    asyncio.run(check())


def test_sse_header_rebase_frame_and_permission_restore():
    async def check():
        journal = EventJournal()
        pending = (event("permission.requested", "approve"),)
        async def disconnected():
            return False
        stream = follow_events(
            journal, cursor=0, reset_reason="stream_restarted",
            events={"permission.requested"}, sanitize=lambda p: {"content": "[redacted]"},
            disconnected=disconnected, pending=pending,
        )
        reset = unpack(await anext(stream))
        assert reset[1]["payload"]["reason"] == "stream_restarted"
        data = unpack(await anext(stream))
        assert data[0] is None and data[1]["payload"]["content"] == "[redacted]"
        await stream.aclose()
    asyncio.run(check())


def test_sse_auth_and_invalid_cursor_rejected_without_streaming(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-long-local-token-123456789012345")
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/sessions/nope/events").status_code == 401
        headers = {"Authorization": "Bearer test-long-local-token-123456789012345"}
        assert client.get("/api/sessions/nope/events", headers=headers).status_code == 404
        sid = client.post("/api/sessions", headers=headers, json={}).json()["id"]
        assert client.get(f"/api/sessions/{sid}/events?after=NaN", headers=headers).status_code == 400
        assert client.get(
            f"/api/sessions/{sid}/events", headers={**headers, "Last-Event-ID": "invalid"},
        ).status_code == 400
