"""Real Postgres/RLS outbox acceptance (no mocks or skipped test cases)."""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory import MemoryScope
from core.persistence.postgres_sessions import PostgresPrivateSessionStore
from core.persistence.postgres_turn_queue import PostgresPrivateTurnQueue


@pytest.fixture
def stores():
    pytest.importorskip("psycopg")
    dsn = os.environ.get("RIDGE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Real PostgreSQL DSN required")
    return PostgresPrivateSessionStore(dsn), PostgresPrivateTurnQueue(dsn)


def test_atomic_outbox_idempotency_and_stale_fence(stores):
    sessions, queue = stores
    scope = MemoryScope("outbox-t", "owner-u", "project-p")
    sid = uuid.uuid4().hex[:12]
    sessions.create(scope, sid)
    try:
        job = queue.enqueue(scope, sid, idempotency_key="outbox-1", payload={"command": "echo ok"})
        assert queue.enqueue(scope, sid, idempotency_key="outbox-1", payload={"command": "echo ok"}) == job
        first = queue.events(scope, sid)
        assert [(e["id"], e["state"], e["fence"]) for e in first] == [(1, "queued", 0)]
        lease = queue.claim(scope, worker_id="worker-1", lease_seconds=10)
        assert lease.id == job
        assert not queue.complete(scope, job, worker_id="stale", fence=lease.fence, result={"ok": "no"})
        assert len(queue.events(scope, sid)) == 2
        assert queue.complete(scope, job, worker_id="worker-1", fence=lease.fence, result={"output": "private"})
        assert not queue.complete(scope, job, worker_id="worker-1", fence=lease.fence, result={})
        events = queue.events(scope, sid)
        assert [(x["id"], x["state"]) for x in events] == [
            (1, "queued"), (2, "running"), (3, "completed"),
        ]
        assert all("payload" not in e and "output" not in str(e) for e in events)
        assert queue.events(scope, sid, after=2) == [events[-1]]
        assert queue.events(scope, sid, after=3) == []
    finally:
        sessions.delete(scope, sid)


def test_event_cursor_is_scoped_to_owner_session_and_revokes_with_delete(stores):
    sessions, queue = stores
    a = MemoryScope("outbox-a", "same-user", "p")
    b = MemoryScope("outbox-b", "same-user", "p")
    sid = uuid.uuid4().hex[:12]
    sessions.create(a, sid)
    sessions.create(b, sid)
    try:
        job = queue.enqueue(a, sid, idempotency_key="owner-a", payload={"x": 1})
        assert queue.events(a, sid)[0]["id"] == 1
        assert queue.events(b, sid) == []
        with pytest.raises(LookupError):
            queue.events(MemoryScope("outbox-c", "same-user", "p"), sid)
        assert queue.cancel(b, job) is False
        assert queue.cancel(a, job) is True
        assert [e["state"] for e in queue.events(a, sid)] == ["queued", "cancelled"]
        assert queue.events(b, sid) == []
        sessions.delete(a, sid)
        with pytest.raises(LookupError):
            queue.events(a, sid)
    finally:
        sessions.delete(a, sid)
        sessions.delete(b, sid)


def test_concurrent_outbox_sequence_is_strict_and_nonduplicating(stores):
    sessions, queue = stores
    scope = MemoryScope("outbox-c", "user-u", "project-p")
    sid = uuid.uuid4().hex[:12]
    sessions.create(scope, sid)
    try:
        def add(i):
            return queue.enqueue(scope, sid, idempotency_key=f"concurrent-{i}", payload={"i": i})
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(add, range(20)))
        events = queue.events(scope, sid, limit=100)
        assert len(jobs) == len(set(jobs)) == 20
        assert len(events) == 20
        assert [e["id"] for e in events] == list(range(1, 21))
        assert {e["turn_id"] for e in events} == set(jobs)
        assert all(e["state"] == "queued" for e in events)
    finally:
        sessions.delete(scope, sid)


def test_expiry_and_fenced_failure_emit_only_terminal_events(stores):
    sessions, queue = stores
    scope = MemoryScope("outbox-d", "user-u", "project-p")
    sid = uuid.uuid4().hex[:12]
    sessions.create(scope, sid)
    try:
        job = queue.enqueue(scope, sid, idempotency_key="expiry", payload={"x": 1})
        lease = queue.claim(scope, worker_id="first", lease_seconds=1)
        time.sleep(1.15)
        assert queue.reap_expired(scope) == 1
        assert not queue.complete(scope, job, worker_id="first", fence=lease.fence, result={"ok": True})
        assert queue.claim(scope, worker_id="second") is None
        assert [(e["state"], e["fence"]) for e in queue.events(scope, sid)] == [
            ("queued", 0), ("running", 1), ("failed", 2),
        ]
    finally:
        sessions.delete(scope, sid)


def test_outbox_is_immutable_to_runtime_role(stores):
    sessions, queue = stores
    scope = MemoryScope("outbox-e", "user-u", "project-p")
    sid = uuid.uuid4().hex[:12]
    sessions.create(scope, sid)
    try:
        queue.enqueue(scope, sid, idempotency_key="immutable", payload={"x": 1})
        with pytest.raises(Exception):
            with queue._connect() as conn:
                with conn.cursor() as cur:
                    queue._scope(cur, scope)
                    cur.execute(
                        """DELETE FROM ridge.private_turn_events
                            WHERE tenant_id=%s AND project_id=%s
                              AND owner_user_id=%s""",
                        (scope.tenant_id, scope.project_id, scope.user_id),
                    )
        assert len(queue.events(scope, sid)) == 1
    finally:
        sessions.delete(scope, sid)


def test_replay_validates_cursor_without_database_mutation(stores):
    sessions, queue = stores
    scope = MemoryScope("outbox-f", "user-u", "project-p")
    sid = uuid.uuid4().hex[:12]
    sessions.create(scope, sid)
    try:
        for bad in (True, -1, 2**63, "5"):
            with pytest.raises(ValueError):
                queue.events(scope, sid, after=bad)
        for bad in (0, 101, True):
            with pytest.raises(ValueError):
                queue.events(scope, sid, limit=bad)
    finally:
        sessions.delete(scope, sid)
