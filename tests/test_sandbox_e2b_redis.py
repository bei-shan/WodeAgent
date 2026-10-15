"""Redis E2B capacity scripts: atomic admission, owner fencing, no silent TTL."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.e2b_redis_capacity import (
    RedisE2BCapacity, _RESERVE, _TRACK, _RELEASE,
)
from core.sandbox.e2b_provider import E2BCommandProvider


class FakeRedis:
    """Atomic in-memory script simulator; live Redis Lua is separately gated."""
    def __init__(self):
        self.lock = threading.Lock()
        self.h: dict[str, dict[str, str]] = {}
        self.limits: dict[str, str] = {}
        self.fail = False
        self.scripts = []

    def eval(self, script, keys_count, slots, limit_key, token, owner, *args):
        assert keys_count == 2
        self.scripts.append(script)
        with self.lock:
            if self.fail:
                raise OSError("Redis timeout")
            data = self.h.setdefault(slots, {})
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
                return "RELEASED"
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
    with pytest.raises(SandboxUnavailable, match="unavailable"):
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
