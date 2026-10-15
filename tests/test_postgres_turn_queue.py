"""Real PostgreSQL durable-turn claims, replay and fencing regression."""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory import MemoryScope
from core.persistence import PostgresPrivateSessionStore
from core.persistence.postgres_turn_queue import PostgresPrivateTurnQueue, TurnIdempotencyConflict


@pytest.fixture
def stores():
    pytest.importorskip("psycopg")
    dsn = os.environ.get("RIDGE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Live PostgreSQL DSN required")
    return PostgresPrivateSessionStore(dsn), PostgresPrivateTurnQueue(dsn)


def test_queue_scoped_idempotency_and_fencing(stores):
    session_store, queue = stores
    owner = MemoryScope("owner-t", "owner-u", "owner-p")
    outsider = MemoryScope("other-t", "owner-u", "owner-p")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    try:
        one = queue.enqueue(owner, sid, idempotency_key="request-123", payload={"text": "test"})
        assert queue.enqueue(owner, sid, idempotency_key="request-123", payload={"text": "test"}) == one
        with pytest.raises(TurnIdempotencyConflict):
            queue.enqueue(owner, sid, idempotency_key="request-123", payload={"text": "different"})
        with pytest.raises(TurnIdempotencyConflict):
            queue.enqueue(owner, "ffffffffffff", idempotency_key="request-123", payload={"text": "test"})
        assert queue.claim(outsider, worker_id="w-other") is None
        leased = queue.claim(owner, worker_id="worker-A")
        assert leased.id == one and leased.fence == 1 and leased.attempt == 1
        assert leased.payload == {"text": "test"}
        assert queue.claim(owner, worker_id="worker-B") is None
        assert not queue.complete(outsider, one, worker_id="worker-A", fence=1, result={})
        assert not queue.complete(owner, one, worker_id="worker-B", fence=1, result={})
        assert queue.renew(owner, one, worker_id="worker-A", fence=1, lease_seconds=60)
        assert queue.complete(owner, one, worker_id="worker-A", fence=1, result={"ok": True})
        assert not queue.complete(owner, one, worker_id="worker-A", fence=1, result={})
        assert queue.claim(owner, worker_id="worker-C") is None
    finally:
        session_store.delete(owner, sid)


def test_expired_lease_is_recovered_without_stale_commit(stores):
    session_store, queue = stores
    owner = MemoryScope("lease-t", "lease-u", "lease-p")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    try:
        key = queue.enqueue(owner, sid, idempotency_key="lease-id", payload={"type": "read-only"})
        first = queue.claim(owner, worker_id="old", lease_seconds=1)
        assert first.id == key
        time.sleep(1.15)
        second = queue.claim(owner, worker_id="new", lease_seconds=10)
        assert second is not None
        assert second.id == first.id
        assert second.fence == first.fence + 1
        assert second.attempt == 2
        assert not queue.renew(owner, key, worker_id="old", fence=first.fence)
        assert not queue.complete(owner, key, worker_id="old", fence=first.fence, result={"stale": True})
        assert queue.complete(owner, key, worker_id="new", fence=second.fence, result={"done": True})
    finally:
        session_store.delete(owner, sid)


def test_concurrent_claim_is_atomic_and_no_job_runs_twice(stores):
    session_store, queue = stores
    owner = MemoryScope("many-t", "many-u", "many-p")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    count = 24
    try:
        ids = [
            queue.enqueue(owner, sid, idempotency_key=f"job-{i}", payload={"index": i})
            for i in range(count)
        ]

        def claim(i):
            return queue.claim(owner, worker_id=f"worker-{i}")

        with ThreadPoolExecutor(max_workers=12) as pool:
            received = list(pool.map(claim, range(36)))
        leased = [x for x in received if x is not None]
        assert len(leased) == count
        assert {x.id for x in leased} == set(ids)
        assert all(x.attempt == 1 and x.fence == 1 for x in leased)
        for i, leased_turn in enumerate(leased):
            assert queue.complete(owner, leased_turn.id, worker_id=f"worker-{received.index(leased_turn)}",
                                  fence=leased_turn.fence, result={"done": i})
        assert queue.claim(owner, worker_id="late") is None
    finally:
        session_store.delete(owner, sid)


def test_enqueue_does_not_accept_user_selected_other_scope(stores):
    session_store, queue = stores
    owner = MemoryScope("primary", "alice", "app")
    attacker = MemoryScope("secondary", "alice", "app")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    try:
        with pytest.raises(Exception):
            queue.enqueue(attacker, sid, idempotency_key="forbidden", payload={"text": "x"})
        assert queue.claim(attacker, worker_id="attacker") is None
    finally:
        session_store.delete(owner, sid)


def test_cancel_revokes_active_fence_and_is_scoped(stores):
    session_store, queue = stores
    owner = MemoryScope("cancel-t", "cancel-u", "cancel-p")
    outsider = MemoryScope("other-t", "cancel-u", "cancel-p")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    try:
        queued = queue.enqueue(owner, sid, idempotency_key="cancel-queued", payload={"x": 1})
        assert queue.cancel(outsider, queued) is False
        assert queue.cancel(owner, queued) is True
        assert queue.cancel(owner, queued) is False

        running = queue.enqueue(owner, sid, idempotency_key="cancel-running", payload={"x": 2})
        lease = queue.claim(owner, worker_id="worker-cancel", lease_seconds=10)
        assert lease is not None and lease.id == running
        assert queue.cancel(owner, running)
        assert not queue.renew(owner, running, worker_id="worker-cancel", fence=lease.fence)
        assert not queue.complete(owner, running, worker_id="worker-cancel",
                                  fence=lease.fence, result={"done": True})
        assert queue.claim(owner, worker_id="worker-after-cancel") is None
        with pytest.raises(ValueError):
            queue.cancel(owner, "../bogus-turn")
    finally:
        session_store.delete(owner, sid)


def test_last_expired_attempt_is_marked_terminal_failed(stores):
    session_store, queue = stores
    owner = MemoryScope("exhaust-t", "exhaust-u", "exhaust-p")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    try:
        turn = queue.enqueue(owner, sid, idempotency_key="crash-thrice", payload={"secret": "don't echo"})
        for attempt in range(1, 4):
            lease = queue.claim(owner, worker_id=f"lost-{attempt}", lease_seconds=10)
            assert lease is not None and lease.attempt == attempt
            # Simulate a crash + an expired DB-authoritative lease (no wall-clock sleep).
            with queue._connect() as conn:
                with conn.cursor() as cur:
                    queue._scope(cur, owner)
                    cur.execute(
                        """UPDATE ridge.private_turn_jobs
                              SET lease_until=now()-interval '1 second'
                            WHERE tenant_id=%s AND project_id=%s
                              AND owner_user_id=%s AND turn_id=%s""",
                        (owner.tenant_id, owner.project_id, owner.user_id, turn),
                    )
        assert queue.claim(owner, worker_id="no-more-retries") is None
        with queue._connect() as conn:
            with conn.cursor() as cur:
                queue._scope(cur, owner)
                cur.execute(
                    """SELECT status, result, fence, lease_until
                         FROM ridge.private_turn_jobs
                        WHERE tenant_id=%s AND project_id=%s
                          AND owner_user_id=%s AND turn_id=%s""",
                    (owner.tenant_id, owner.project_id, owner.user_id, turn),
                )
                status, result, fence, expiry = cur.fetchone()
        assert status == "failed"
        assert result == {"error_kind": "retry_exhausted"}
        assert expiry is None
        assert fence == 4
        assert not queue.complete(owner, turn, worker_id="lost-3", fence=3, result={})
        assert queue.reap_exhausted(owner) == 0
        assert queue.reap_exhausted(MemoryScope("other-t", "exhaust-u", "exhaust-p")) == 0
    finally:
        session_store.delete(owner, sid)


def test_concurrent_replayed_idempotency_key_is_consistent(stores):
    session_store, queue = stores
    owner = MemoryScope("race-t", "race-u", "race-p")
    sid = uuid.uuid4().hex[:12]
    session_store.create(owner, sid)
    try:
        def send(_):
            return queue.enqueue(owner, sid, idempotency_key="exact-same",
                                 payload={"text": "same", "v": 1})
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(send, range(24)))
        assert len(set(ids)) == 1
        with pytest.raises(TurnIdempotencyConflict):
            queue.enqueue(owner, sid, idempotency_key="exact-same", payload={"v": 2})
    finally:
        session_store.delete(owner, sid)
