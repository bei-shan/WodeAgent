"""Deterministic local admission, cancellation and HTTP overload regressions.

No real LLM required. Tenant owner keys are synthetic *trusted* identifiers
in this unit test only, not caller-supplied HTTP headers.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.runtime.admission import TurnAdmission, TurnCapacityExceeded
from core.runtime.session_controller import AgentSession
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent


def _wait_for_idle(session: AgentSession) -> None:
    assert session._thread is not None
    session._thread.join(timeout=4)
    assert not session._thread.is_alive()


@pytest.mark.parametrize("limit,per_owner", [(0, None), (257, None), (1, 0), (2, 3), (True, None)])
def test_invalid_capacity_configuration_fails_closed(limit, per_owner):
    with pytest.raises(ValueError):
        TurnAdmission(max_active=limit, max_per_owner=per_owner)


def test_owner_and_global_limits_and_double_release():
    admission = TurnAdmission(max_active=3, max_per_owner=2)
    first = admission.acquire("tenant-a")
    second = admission.acquire("tenant-a")
    with pytest.raises(TurnCapacityExceeded):
        admission.acquire("tenant-a")
    third = admission.acquire("tenant-b")
    with pytest.raises(TurnCapacityExceeded):
        admission.acquire("tenant-c")
    assert admission.snapshot().active == 3
    assert admission.snapshot().rejected == 2

    first.release()
    first.release()
    assert admission.snapshot().active == 2
    next_turn = admission.acquire("tenant-c")
    assert admission.snapshot().active == 3
    for permit in (second, third, next_turn):
        permit.release()
    assert admission.snapshot().active == 0


def test_shared_local_capacity_under_parallel_contention(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    admission = TurnAdmission(max_active=3)
    gate = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    running = 0

    def execute(_content):
        nonlocal running
        with lock:
            running += 1
            if running >= 3:
                started.set()
        gate.wait(timeout=4)
        return "done"

    sessions = []
    for i in range(32):
        session = AgentSession(f"{i:012x}", lambda: None, admission=admission)
        session._agent = SimpleNamespace(run=execute)
        sessions.append(session)

    def launch(session):
        try:
            return session.send_message("work")
        except TurnCapacityExceeded:
            return False

    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(launch, sessions))
        assert sum(results) == 3
        assert started.wait(timeout=2)
        assert admission.snapshot().active == 3
        assert admission.snapshot().rejected == 29
    finally:
        gate.set()
        for session in sessions:
            if session._thread is not None:
                _wait_for_idle(session)
    assert admission.snapshot().active == 0
    assert not any(session.busy for session in sessions)


def test_cold_agent_factory_failure_releases_permit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    admission = TurnAdmission(max_active=1)
    session = AgentSession("123456789abc", lambda: None, admission=admission)

    def bad_init():
        raise RuntimeError("agent config unavailable")

    monkeypatch.setattr(session, "ensure_agent", bad_init)
    with pytest.raises(RuntimeError, match="agent config unavailable"):
        session.send_message("hello")
    assert not session.busy
    assert admission.snapshot().active == 0


def test_worker_thread_start_failure_releases_permit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    admission = TurnAdmission(max_active=1)
    session = AgentSession("123456789abc", lambda: None, admission=admission)
    session._agent = SimpleNamespace(run=lambda _: "done")

    def fail_start(self):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread creation failed"):
        session.send_message("hello")
    assert not session.busy
    assert admission.snapshot().active == 0


def test_agent_failure_still_releases_capacity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    admission = TurnAdmission(max_active=1)
    session = AgentSession("123456789abc", lambda: None, admission=admission)

    def fail_run(_text):
        raise RuntimeError("safe simulated failure")

    session._agent = SimpleNamespace(run=fail_run)
    assert session.send_message("hello")
    _wait_for_idle(session)
    assert session.last_turn.status.value == "failed"
    assert admission.snapshot().active == 0


class BlockingAgent(FakeAgent):
    entered = threading.Event()
    unblock = threading.Event()

    def run(self, content):
        type(self).entered.set()
        type(self).unblock.wait(timeout=4)
        return super().run(content)


def test_http_429_before_second_agent_initialization_and_recovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RIDGE_MAX_ACTIVE_TURNS", "1")
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    BlockingAgent.entered = threading.Event()
    BlockingAgent.unblock = threading.Event()
    app = create_app(agent_factory=BlockingAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        first = client.post("/api/sessions", json={}).json()["id"]
        second = client.post("/api/sessions", json={}).json()["id"]
        try:
            assert client.post(f"/api/sessions/{first}/messages",
                               json={"content": "long"}).status_code == 200
            assert BlockingAgent.entered.wait(timeout=2)
            response = client.post(f"/api/sessions/{second}/messages",
                                   json={"content": "should not run"})
            assert response.status_code == 429
            assert response.headers["retry-after"] == "1"
            assert client.get(f"/api/sessions/{second}").json()["busy"] is False
            usage = client.get("/api/health").json()["turn_admission"]
            assert usage == {"active": 1, "limit": 1, "rejected": 1}
        finally:
            BlockingAgent.unblock.set()
        deadline = time.monotonic() + 3
        while app.state.controller.admission.snapshot().active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.state.controller.admission.snapshot().active == 0
        assert client.post(f"/api/sessions/{second}/messages",
                           json={"content": "retry"}).status_code == 200
        second_session = app.state.controller.get_session(second)
        _wait_for_idle(second_session)
        assert app.state.controller.admission.snapshot().active == 0


def test_web_capacity_configuration_rejects_malformed_values(tmp_path, monkeypatch):
    for bad in ("zero", "0", "-1", "257"):
        monkeypatch.setenv("RIDGE_MAX_ACTIVE_TURNS", bad)
        with pytest.raises(ValueError, match="(RIDGE_MAX_ACTIVE_TURNS|max_active)"):
            create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
