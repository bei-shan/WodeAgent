"""E2B audit reports observations, never deletes/steals remote resources."""
from __future__ import annotations

import threading
import time

import pytest

from core.sandbox.e2b_redis_capacity import RedisE2BCapacity
from core.sandbox.e2b_reconciliation import E2BReconciliationAuditor
from tests.test_sandbox_e2b_redis import FakeRedis, token


class AuditRedis(FakeRedis):
    def hscan(self, key, cursor=0, count=64):
        if self.fail:
            raise OSError("Redis timeout")
        if key.endswith(":commands"):
            values = self.commands.get(key, {})
        else:
            values = self.h.get(key, {})
        pairs = sorted(values.items())
        start = int(cursor)
        batch = dict(pairs[start:start + count])
        following = start + count
        return (following if following < len(pairs) else 0), batch


def make_capacity():
    db = AuditRedis()
    return db, RedisE2BCapacity(
        deployment="audit-test", limit=3, redis_client=db,
    )


def remote_entry(session, remote):
    return {
        "sandbox_id": remote,
        "metadata": {
            "ridge_provider": "e2b_experimental",
            "ridge_session": session,
        },
    }


def test_audit_matching_remote_inventory_and_safe_fingerprints():
    db, capacity = make_capacity()
    t = token(1)
    capacity.reserve(t)
    capacity.track(t, "remote-vm-001")
    command = capacity.begin_command(t, "remote-vm-001")
    report = E2BReconciliationAuditor(capacity).audit(
        remote_inventory=lambda: [remote_entry(t, "remote-vm-001")],
    )
    assert report.schema_version == 1
    assert report.redis_complete and report.remote_status == "complete"
    assert report.tracked == 1 and report.pending == 0
    assert report.execution_claims == 1 and report.malformed_records == 0
    assert report.possible_missing_remote == 0
    assert report.possible_untracked_remote == 0
    serialized = str(report.to_dict())
    assert "remote-vm-001" not in serialized
    assert capacity._owner not in serialized
    assert t not in serialized
    assert len(report.vm_fingerprints[0]) == 12
    assert capacity.used == 1
    capacity.end_command(t, "remote-vm-001", command)
    capacity.release(t)


def test_audit_only_identifies_possible_discrepancies_not_deletion():
    _, capacity = make_capacity()
    cap_token = token(3)
    capacity.reserve(cap_token)
    capacity.track(cap_token, "remote-vm-001")
    report = E2BReconciliationAuditor(capacity).audit(
        remote_inventory=lambda: [
            remote_entry(cap_token, "remote-vm-other"),
            remote_entry(token(4), "remote-vm-999"),
        ],
    )
    assert report.possible_missing_remote == 1
    assert report.possible_untracked_remote == 2
    assert capacity.used == 1
    capacity.release(cap_token)


def test_incomplete_remote_inventory_cannot_assert_missing_or_orphan():
    _, capacity = make_capacity()
    t = token(1)
    capacity.reserve(t)
    capacity.track(t, "remote-vm-001")
    report = E2BReconciliationAuditor(capacity).audit(
        remote_inventory=lambda: [{"unrelated": "not an E2B sandbox"}],
    )
    assert report.remote_status == "incomplete"
    assert report.possible_missing_remote is None
    assert report.possible_untracked_remote is None
    assert capacity.used == 1
    capacity.release(t)


def test_bounded_partial_scan_and_redis_outage_remain_read_only():
    db, capacity = make_capacity()
    for i in range(25):
        db.h.setdefault(capacity._key, {})[token(i)] = (
            capacity._owner + ":remote-vm-001"
        )
    report = E2BReconciliationAuditor(
        capacity, max_pages=1, max_items=8,
    ).audit()
    assert not report.redis_complete
    assert report.tracked <= 8
    assert report.possible_missing_remote is None
    assert len(db.h[capacity._key]) == 25
    db.fail = True
    report = E2BReconciliationAuditor(capacity).audit()
    assert not report.redis_complete
    assert report.tracked == 0
    assert report.possible_untracked_remote is None
    assert len(db.h[capacity._key]) == 25


def test_remote_inventory_hang_bounded_and_cannot_misclassify():
    _, capacity = make_capacity()
    t = token(1)
    capacity.reserve(t)
    capacity.track(t, "remote-vm-001")
    started = threading.Event()
    blocked = threading.Event()
    def slow_inventory():
        started.set()
        blocked.wait(timeout=3)
        return [remote_entry(t, "remote-vm-001")]
    try:
        started_at = time.monotonic()
        report = E2BReconciliationAuditor(
            capacity, max_seconds=0.1,
        ).audit(remote_inventory=slow_inventory)
        assert time.monotonic() - started_at < 1.5
        assert report.remote_status == "incomplete"
        assert report.possible_missing_remote is None
    finally:
        blocked.set()
        capacity.release(t)


@pytest.mark.parametrize("invalid", [
    {"max_pages": 0}, {"max_items": 1000}, {"max_seconds": -1},
    {"max_seconds": True},
])
def test_invalid_limits_rejected(invalid):
    _, cap = make_capacity()
    with pytest.raises(ValueError):
        E2BReconciliationAuditor(cap, **invalid)
