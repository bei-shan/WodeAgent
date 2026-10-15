"""Real Redis atomic multi-instance capacity, expiry and fail-closed tests."""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory import MemoryScope
from core.runtime.distributed_admission import (
    DistributedAdmissionUnavailable,
    DistributedTurnCapacityExceeded,
    RedisTurnAdmission,
)


def scope(t="tenant", u="user", p="project"):
    return MemoryScope(t, u, p)


@pytest.fixture
def instance():
    redis = pytest.importorskip("redis")
    url = os.environ.get("RIDGE_TEST_REDIS_URL")
    if not url:
        pytest.skip("Real Redis test URL required")
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=3)
    client.ping()
    namespace = "test-" + uuid.uuid4().hex
    key = b"test-permit-hmac-key-0123456789abcdef"
    def create(**opts):
        return RedisTurnAdmission(
            client, hmac_key=key, namespace=namespace, **opts,
        )
    yield client, create
    for redis_key in client.scan_iter("{" + namespace + "}:turns:*"):
        client.delete(redis_key)


def test_atomic_global_and_tenant_limits_across_clients(instance):
    _, create = instance
    config = dict(max_global=3, max_tenant=2, max_user=1, max_project=2)
    a, b = create(**config), create(**config)
    pa = a.acquire(scope("one", "alice"))
    pb = b.acquire(scope("one", "bob"))
    with pytest.raises(DistributedTurnCapacityExceeded):
        a.acquire(scope("one", "charlie"))
    pc = b.acquire(scope("two", "alice"))
    with pytest.raises(DistributedTurnCapacityExceeded):
        a.acquire(scope("three", "new"))
    assert pa.renew() is True
    pa.release()
    pa.release()
    pnew = b.acquire(scope("one", "charlie"))
    assert pnew.renew() is True
    for permit in (pb, pc, pnew):
        permit.release()


def test_concurrent_acquire_has_global_bound_without_oversubscription(instance):
    _, create = instance
    clients = [create(max_global=5, max_tenant=5, max_user=5, max_project=5) for _ in range(6)]
    acquired = []

    def attempt(i):
        try:
            return clients[i % len(clients)].acquire(scope(f"t{i}", f"u{i}", f"p{i}"))
        except DistributedTurnCapacityExceeded:
            return None

    with ThreadPoolExecutor(max_workers=24) as pool:
        acquired = list(pool.map(attempt, range(60)))
    permits = [p for p in acquired if p is not None]
    try:
        assert len(permits) == 5
    finally:
        for p in permits:
            p.release()


def test_expired_stale_permit_cannot_renew(instance):
    redis, create = instance
    a = create(max_global=1, max_tenant=1, max_user=1, max_project=1)
    s = scope()
    p = a.acquire(s)
    # Simulate expiration deterministically with Redis's own ZSET clock.
    keys = a._keys(s)
    for k in keys:
        redis.zadd(k, {p._token: 1})
    assert p.renew() is False
    another = a.acquire(s)
    try:
        assert another.renew() is True
    finally:
        p.release()
        another.release()


def test_scoped_keys_hide_raw_ids_and_use_one_cluster_slot(instance):
    _, create = instance
    a = create()
    keys = a._keys(scope("rawtenant", "alice", "secretproject"))
    assert len(keys) == 4
    assert len({x.split("}")[0] for x in keys}) == 1
    assert all("rawtenant" not in x and "alice" not in x and "secretproject" not in x
               for x in keys)


def test_redis_failure_fails_closed_without_local_fallback():
    class Broken:
        def eval(self, *args):
            raise ConnectionError("offline")

    a = RedisTurnAdmission(Broken(), hmac_key=b"x" * 32)
    with pytest.raises(DistributedAdmissionUnavailable, match="unavailable"):
        a.acquire(scope())


def test_invalid_limits_and_scopes_rejected():
    class Client:
        def eval(self, *args):
            return 1

    with pytest.raises(ValueError):
        RedisTurnAdmission(Client(), hmac_key=b"bad")
    with pytest.raises(ValueError):
        RedisTurnAdmission(Client(), hmac_key=b"x" * 32, max_global=2, max_tenant=3)
    a = RedisTurnAdmission(Client(), hmac_key=b"x" * 32)
    with pytest.raises(TypeError):
        a.acquire(None)
