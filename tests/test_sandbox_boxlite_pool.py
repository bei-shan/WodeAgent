"""BoxLite warm sessions: private loop, identity, capacity, expiry and fail-closed stop."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from core.sandbox.boxlite_pool import BoxliteSessionPool
from core.sandbox.boxlite_provider import BoxliteCommandProvider
from core.sandbox.contracts import CommandResult, SandboxUnavailable


class FakeBox:
    created = []
    stopped = []
    exec_threads = []
    fail_start = False

    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "test")
        self.created.append(self.name)

    async def __aenter__(self):
        if self.fail_start:
            raise RuntimeError("simulated VM boot failure")
        self.exec_threads.append(threading.get_ident())
        return self

    async def __aexit__(self, *args):
        self.stopped.append(self.name)

    async def exec(self, *argv, **kwargs):
        self.exec_threads.append(threading.get_ident())
        if "sleep" in argv[-1]:
            await asyncio.sleep(60)
        return SimpleNamespace(stdout="guest-ok", stderr="", exit_code=0)


@pytest.fixture(autouse=True)
def clear_box_state():
    FakeBox.created.clear()
    FakeBox.stopped.clear()
    FakeBox.exec_threads.clear()
    FakeBox.fail_start = False


def _pool(*, capacity=2, ttl=60, now=None, factory=FakeBox):
    return BoxliteSessionPool(
        factory, max_replicas=capacity, idle_timeout=ttl,
        clock=(lambda: now[0]) if now is not None else __import__("time").monotonic,
    )


def test_released_vm_reused_for_its_own_session_only():
    p = _pool()
    try:
        assert p.acquire("session-a") == "session-a"
        assert p.counts == (1, 0, 0)
        async def eval_command(box):
            raw = await box.exec("sh", "-lc", "echo good")
            return CommandResult(raw.stdout, raw.stderr, raw.exit_code)
        first = p.execute("session-a", eval_command, timeout_seconds=3)
        assert first.stdout == "guest-ok"
        p.release("session-a")
        assert p.counts == (0, 1, 0)
        p.acquire("session-a")
        assert len(FakeBox.created) == 1
        p.release("session-a")
        p.acquire("session-b")
        assert len(FakeBox.created) == 2
        assert p.counts == (1, 1, 0)
    finally:
        p.shutdown()
    assert len(FakeBox.stopped) == 2
    assert not p._thread.is_alive()
    assert not p._reaper.is_alive()
    assert len(set(FakeBox.exec_threads)) == 1


def test_capacity_full_fails_closed_and_warm_lru_eviction():
    p = _pool(capacity=1)
    try:
        p.acquire("A")
        with pytest.raises(SandboxUnavailable, match="capacity"):
            p.acquire("B")
        p.release("A")
        p.acquire("B")
        assert len(FakeBox.created) == 2
        assert FakeBox.stopped == ["test"]
        assert p.counts == (1, 0, 0)
        with pytest.raises(SandboxUnavailable):
            p.acquire("A")
    finally:
        p.shutdown()


def test_scope_cannot_be_acquired_twice_or_execute_without_lease():
    p = _pool()
    try:
        p.acquire("A")
        with pytest.raises(SandboxUnavailable, match="already acquired"):
            p.acquire("A")
        with pytest.raises(SandboxUnavailable, match="exclusive"):
            p.execute("B", lambda box: box.exec("echo"), timeout_seconds=1)
        p.release("A")
        with pytest.raises(SandboxUnavailable, match="exclusive"):
            p.execute("A", lambda box: box.exec("echo"), timeout_seconds=1)
    finally:
        p.shutdown()


def test_idle_ttl_reaper_never_evicts_active_session():
    now = [10.0]
    p = _pool(ttl=5, now=now)
    try:
        p.acquire("A")
        p.acquire("B")
        p.release("B")
        now[0] = 30.0
        assert p.reap_idle() == 1
        assert p.counts == (1, 0, 0)
        assert len(FakeBox.stopped) == 1
        p.release("A")
        now[0] = 31
        assert p.reap_idle() == 0
        now[0] = 40
        assert p.reap_idle() == 1
    finally:
        p.shutdown()


def test_failed_startup_releases_capacity_and_never_falls_back():
    class FailOnce(FakeBox):
        attempted = False
        async def __aenter__(self):
            if not type(self).attempted:
                type(self).attempted = True
                raise OSError("guest failed")
            return await super().__aenter__()

    p = _pool(capacity=1, factory=FailOnce)
    try:
        with pytest.raises(SandboxUnavailable):
            p.acquire("A")
        assert p.counts == (0, 0, 0)
        assert p.acquire("B") == "B"
        p.release("B")
    finally:
        p.shutdown()


def test_cancelled_vm_is_disposed_not_warmed(tmp_path):
    p = BoxliteCommandProvider(tmp_path, box_factory=FakeBox, network_factory=dict)
    p.enable_warm_pool(session_scope="private-session", idle_timeout=60)
    try:
        assert p.execute("echo first").exit_code == 0
        assert p.execute("echo second").exit_code == 0
        assert len(FakeBox.created) == 1
        started = threading.Event()
        stop = threading.Event()
        prior_exec = FakeBox.exec

        async def slow_exec(self, *a, **kw):
            started.set()
            await asyncio.sleep(60)

        FakeBox.exec = slow_exec
        result = []
        worker = threading.Thread(target=lambda: result.append(
            p.execute("sleep 60", cancel_event=stop, timeout_seconds=20),
        ))
        try:
            worker.start()
            assert started.wait(3)
            stop.set()
            worker.join(5)
            assert not worker.is_alive()
            assert result[0].interrupted
            assert p._session_pool.counts == (0, 0, 0)
        finally:
            FakeBox.exec = prior_exec
            stop.set()
            worker.join(timeout=3)
        assert p.execute("echo new").exit_code == 0
        assert len(FakeBox.created) == 2
    finally:
        p.close()


def test_pool_rejects_invalid_limits_and_after_shutdown():
    for params in ({"max_replicas": 0}, {"max_replicas": True},
                   {"idle_timeout": -1}, {"idle_timeout": float("inf")}):
        with pytest.raises(ValueError):
            BoxliteSessionPool(FakeBox, **params)
    p = _pool()
    p.shutdown()
    p.shutdown()
    with pytest.raises(SandboxUnavailable):
        p.acquire("A")


def test_agent_shutdown_closes_owned_vm_pool(tmp_path, monkeypatch):
    from core.config import Config
    from agents.codeAgent import CodeAgent
    from tests.test_e2e_agent import ScriptedMockLLM, make_response
    from tools.registry import ToolRegistry

    provider = BoxliteCommandProvider(tmp_path, box_factory=FakeBox, network_factory=dict)
    monkeypatch.setattr(
        "core.sandbox.boxlite_provider.BoxliteCommandProvider", lambda *a, **kw: provider,
    )
    agent = CodeAgent(
        "pooled-agent", ScriptedMockLLM([make_response(content="ok")]),
        ToolRegistry(), str(tmp_path),
        config=Config(
            sandbox_mode="boxlite", sandbox_boxlite_warm_enabled=True,
            llm_streaming=False, enable_agent_teams=False,
            show_react_steps=False, show_progress=False,
        ),
    )
    assert provider._session_pool is not None
    assert provider.execute("echo warm").exit_code == 0
    agent.close()
    assert provider._session_pool._closed
    assert provider._session_pool.counts == (0, 0, 0)
