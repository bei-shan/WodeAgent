"""Redis E2B capacity scripts: atomic admission, owner fencing, no silent TTL."""
from __future__ import annotations

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.e2b_redis_capacity import (
    RedisE2BCapacity, _RESERVE, _TRACK, _RELEASE,
    _COMMAND_BEGIN, _COMMAND_END,
)
from core.sandbox.e2b_provider import E2BCommandProvider


class FakeRedis:
    """Atomic in-memory script simulator; live Redis Lua is separately gated."""
    def __init__(self):
        self.lock = threading.Lock()
        self.h: dict[str, dict[str, str]] = {}
        self.limits: dict[str, str] = {}
        self.commands: dict[str, dict[str, str]] = {}
        self.fail = False
        self.scripts = []

    def eval(self, script, keys_count, slots, limit_key, command_key, token, owner, *args):
        assert keys_count == 3
        self.scripts.append(script)
        with self.lock:
            if self.fail:
                raise OSError("Redis timeout")
            data = self.h.setdefault(slots, {})
            commands = self.commands.setdefault(command_key, {})
            if script == _RESERVE:
                configured = self.limits.setdefault(limit_key, args[0])
                if configured != args[0]:
                    return "CONFIG_MISMATCH"
                if token in data:
                    return "DUPLICATE"
                if len(data) >= int(args[0]):
                    return "FULL"
                data[token] = owner + ":pending"
                return "GRANTED"
            if script == _TRACK:
                if token not in data:
                    return "MISSING"
                if data[token] not in (owner + ":pending", owner + ":" + args[0]):
                    return "FENCED"
                data[token] = owner + ":" + args[0]
                return "TRACKED"
            if script == _RELEASE:
                if token not in data:
                    return "MISSING"
                if not data[token].startswith(owner + ":"):
                    return "FENCED"
                del data[token]
                commands.pop(token, None)
                return "RELEASED"
            if script == _COMMAND_BEGIN:
                vm_id, command_id = args
                if data.get(token) != owner + ":" + vm_id:
                    return "FENCED"
                if token in commands:
                    return "BUSY"
                commands[token] = owner + ":" + command_id
                return "BEGUN"
            if script == _COMMAND_END:
                vm_id, command_id = args
                if data.get(token) != owner + ":" + vm_id:
                    return "FENCED"
                if commands.get(token) != owner + ":" + command_id:
                    return "FENCED"
                del commands[token]
                return "ENDED"
        raise AssertionError("Unknown Redis E2B Lua script")

    def hlen(self, key):
        if self.fail:
            raise OSError("Redis connection failed")
        return len(self.h.get(key, {}))


def make_store(db, limit=2, deployment="test"):
    return RedisE2BCapacity(
        deployment=deployment, limit=limit, redis_client=db,
    )


def token(i):
    return f"{i:048x}"


def test_reserve_track_release_and_config_fencing():
    db = FakeRedis()
    a, b = make_store(db, limit=1), make_store(db, limit=1)
    a.reserve(token(1))
    assert a.used == b.used == 1
    a.track(token(1), "remote-vm-001")
    assert list(db.h.values())[0][token(1)].endswith(":remote-vm-001")
    with pytest.raises(SandboxUnavailable, match="capacity"):
        b.reserve(token(2))
    with pytest.raises(SandboxUnavailable, match="not confirmed"):
        b.release(token(1))
    with pytest.raises(SandboxUnavailable, match="ownership"):
        a.reserve(token(1))
    with pytest.raises(SandboxUnavailable, match="ownership"):
        make_store(db, limit=2).reserve(token(3))
    a.release(token(1))
    b.reserve(token(2))
    assert b.used == 1
    b.release(token(2))
    assert b.used == 0


def test_parallel_workers_never_allocate_more_than_global_limit():
    db = FakeRedis()
    stores = [make_store(db, limit=3) for _ in range(12)]
    successes = []

    def compete(i):
        try:
            stores[i].reserve(token(i + 1))
            return True
        except SandboxUnavailable:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        successes = list(pool.map(compete, range(12)))
    assert sum(successes) == 3
    assert stores[0].used == 3
    for i, ok in enumerate(successes):
        if ok:
            stores[i].release(token(i + 1))
    assert stores[0].used == 0


def test_durable_reservation_never_auto_expires_and_same_owner_required():
    db = FakeRedis()
    a = make_store(db, limit=1)
    a.reserve(token(1))
    b = make_store(db, limit=1)
    with pytest.raises(SandboxUnavailable):
        b.track(token(1), "remote-vm-001")
    with pytest.raises(SandboxUnavailable):
        b.reserve(token(2))
    assert "EXPIRE" not in _RESERVE.upper()
    assert "PEXPIRE" not in _RESERVE.upper()
    assert "HDEL" not in _RESERVE.upper()
    # Only the exact owner can release; a 'new gateway' cannot free the slot.
    assert b.used == 1
    a.track(token(1), "remote-vm-001")
    a.release(token(1))


def test_redis_connection_failure_refuses_admission_release_and_metrics():
    db = FakeRedis()
    cap = make_store(db)
    db.fail = True
    with pytest.raises(SandboxUnavailable, match="unavailable"):
        cap.reserve(token(1))
    with pytest.raises(SandboxUnavailable, match="cannot be verified"):
        _ = cap.used
    db.fail = False
    cap.reserve(token(1))
    db.fail = True
    with pytest.raises(SandboxUnavailable, match="unavailable"):
        cap.release(token(1))
    assert len(db.h[cap._key]) == 1


@pytest.mark.parametrize("bad", ["", "bad:name", "../escape", "x" * 100])
def test_deployment_identity_validated(bad):
    with pytest.raises(ValueError):
        make_store(FakeRedis(), deployment=bad)


def test_redis_provider_journals_remote_id_only_after_create(tmp_path):
    from tests.test_sandbox_e2b import FakeVM

    class Remote(FakeVM):
        sandbox_id = "remote-vm-001"

    db = FakeRedis()
    capacity = make_store(db, limit=1)
    remote = Remote()
    p = E2BCommandProvider(
        tmp_path, api_key="unit-key", sdk_factory=lambda **_: remote,
        single_gateway_ack=True, capacity=capacity,
    )
    assert capacity.used == 0
    assert p.execute("true").exit_code == 0
    assert capacity.used == 1
    assert db.h[capacity._key][p._slot].endswith(":remote-vm-001")
    p.close()
    assert capacity.used == 0
    assert remote.killed == 1


def test_failed_redis_release_after_remote_kill_leaves_slot_reserved(tmp_path):
    from tests.test_sandbox_e2b import FakeVM

    class Remote(FakeVM):
        sandbox_id = "remote-vm-001"

    db = FakeRedis()
    cap = make_store(db, limit=1)
    remote = Remote()
    p = E2BCommandProvider(
        tmp_path, api_key="unit-key", sdk_factory=lambda **_: remote,
        single_gateway_ack=True, capacity=cap,
    )
    p.execute("true")
    db.fail = True
    p.close()
    assert remote.killed == 1
    db.fail = False
    assert cap.used == 1
    with pytest.raises(SandboxUnavailable):
        make_store(db, limit=1).reserve(token(2))


@pytest.mark.skipif(
    os.getenv("AGENT_E2B_REDIS_INTEGRATION") != "1",
    reason="explicit trusted Redis integration acknowledgement required",
)
def test_real_redis_atomic_capacity_and_fenced_release():
    """Connects to Redis only on opt-in; uses isolated random test keys."""
    url = os.getenv("E2B_REDIS_URL")
    if not url:
        pytest.skip("E2B_REDIS_URL not configured")
    from redis import Redis
    client = Redis.from_url(
        url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3,
    )
    deployment = "accept-" + secrets.token_hex(8)
    a = RedisE2BCapacity(deployment=deployment, limit=1, redis_client=client)
    b = RedisE2BCapacity(deployment=deployment, limit=1, redis_client=client)
    try:
        a.reserve(token(1))
        with pytest.raises(SandboxUnavailable):
            b.reserve(token(2))
        a.track(token(1), "live-test-remote")
        fence = a.begin_command(token(1), "live-test-remote")
        with pytest.raises(SandboxUnavailable):
            a.begin_command(token(1), "live-test-remote")
        with pytest.raises(SandboxUnavailable):
            b.end_command(token(1), "live-test-remote", fence)
        a.end_command(token(1), "live-test-remote", fence)
        with pytest.raises(SandboxUnavailable):
            b.release(token(1))
        a.release(token(1))
        assert b.used == 0
        b.reserve(token(2))
        b.release(token(2))
    finally:
        # These keys are generated for this test alone; never clear a
        # production deployment's shared E2B capacity ledger.
        client.delete(a._key, a._limit_key, a._command_key)
        client.close()


def test_exact_owner_and_execution_token_required_for_command_fences():
    db = FakeRedis()
    owner = make_store(db, limit=1)
    stranger = make_store(db, limit=1)
    t = token(42)
    owner.reserve(t)
    owner.track(t, "remote-vm-001")
    running = owner.begin_command(t, "remote-vm-001")
    assert len(running) == 48
    with pytest.raises(SandboxUnavailable, match="fenced or busy"):
        owner.begin_command(t, "remote-vm-001")
    with pytest.raises(SandboxUnavailable, match="fenced or busy"):
        stranger.begin_command(t, "remote-vm-001")
    with pytest.raises(SandboxUnavailable, match="fenced or unconfirmed"):
        owner.end_command(t, "remote-vm-001", token(99))
    assert db.commands[owner._command_key][t].endswith(running)
    owner.end_command(t, "remote-vm-001", running)
    assert not db.commands[owner._command_key]
    retry = owner.begin_command(t, "remote-vm-001")
    assert retry != running
    # VM destruction is a stronger authority: release clears any stale claim
    # *only* after the caller has confirmed the remote VM was destroyed.
    owner.release(t)
    assert owner.used == 0
    assert not db.commands[owner._command_key]
    with pytest.raises(SandboxUnavailable):
        owner.end_command(t, "remote-vm-001", retry)


def test_execution_claim_survives_owner_crash_and_is_not_stolen():
    db = FakeRedis()
    owner = make_store(db, limit=1)
    t = token(1)
    owner.reserve(t)
    owner.track(t, "remote-vm-001")
    owner.begin_command(t, "remote-vm-001")
    assert "EXPIRE" not in _COMMAND_BEGIN.upper()
    other_process = make_store(db, limit=1)
    with pytest.raises(SandboxUnavailable):
        other_process.begin_command(t, "remote-vm-001")
    with pytest.raises(SandboxUnavailable):
        other_process.release(t)
    with pytest.raises(SandboxUnavailable):
        other_process.reserve(token(2))
    assert other_process.used == 1


def test_concurrent_execution_fences_only_admit_one_thread():
    db = FakeRedis()
    cap = make_store(db, limit=1)
    t = token(2)
    cap.reserve(t)
    cap.track(t, "remote-vm-001")
    def attempt(_):
        try:
            return cap.begin_command(t, "remote-vm-001")
        except SandboxUnavailable:
            return None
    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(attempt, range(16)))
    winners = [v for v in decisions if v is not None]
    assert len(winners) == 1
    cap.end_command(t, "remote-vm-001", winners[0])
    cap.release(t)


def test_provider_completes_command_claim_only_after_success(tmp_path):
    from tests.test_sandbox_e2b import FakeVM
    class Remote(FakeVM):
        sandbox_id = "remote-vm-001"
    db = FakeRedis()
    cap = make_store(db, limit=1)
    vm = Remote()
    from core.sandbox.e2b_provider import E2BCommandProvider
    agent = E2BCommandProvider(
        tmp_path, api_key="fake-secret", single_gateway_ack=True,
        capacity=cap, sdk_factory=lambda **kw: vm,
    )
    try:
        assert agent.execute("echo ok").exit_code == 0
        assert cap.used == 1
        assert not db.commands[cap._command_key]
        assert vm.killed == 0
        assert agent.execute("second").exit_code == 0
        assert not db.commands[cap._command_key]
    finally:
        agent.close()
    assert cap.used == 0
    assert vm.killed == 1


def test_provider_fails_closed_when_redis_fence_cannot_be_verified(tmp_path):
    from tests.test_sandbox_e2b import FakeVM
    class Remote(FakeVM):
        sandbox_id = "remote-vm-001"
    db = FakeRedis()
    cap = make_store(db, limit=1)
    vm = Remote()
    from core.sandbox.e2b_provider import E2BCommandProvider
    p = E2BCommandProvider(
        tmp_path, api_key="fake-secret", single_gateway_ack=True,
        capacity=cap, sdk_factory=lambda **kw: vm,
    )
    assert p.execute("first").exit_code == 0
    db.fail = True
    with pytest.raises(SandboxUnavailable, match="fence admission"):
        p.execute("must-not-run")
    assert len(vm.commands.calls) == 1
    assert p._poisoned
    db.fail = False
    # Depending on kill outcome, slot is either safely released or retained;
    # never replay when Redis result is uncertain.
    with pytest.raises(SandboxUnavailable):
        p.execute("must-not-run")
