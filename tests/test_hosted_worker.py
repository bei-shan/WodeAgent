"""Hosted worker: capacity-before-claim, scoped identity, cancellation and fencing."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory.contracts import MemoryScope
from core.persistence.postgres_turn_queue import LeasedTurn
from core.runtime.hosted_worker import (
    HostedLeaseLost, HostedTurnWorker, HostedWorkerUnavailable,
)
from desktop.service.hosted_identity import HostedIdentity


OWNER = MemoryScope("tenant-one", "same-user", "project")
OTHER = MemoryScope("tenant-two", "same-user", "project")


class FakeMembership:
    def __init__(self):
        self.active = {OWNER}
        self.fail = False
        self.checked = []

    def authorize_membership(self, scope):
        self.checked.append(scope)
        if self.fail:
            raise ConnectionError("unavailable")
        return scope in self.active


class FakePermit:
    def __init__(self):
        self.released = False
        self.renewed = 0
        self.invalid = False

    def renew(self):
        self.renewed += 1
        return not self.invalid

    def release(self):
        self.released = True


class FakeAdmission:
    def __init__(self):
        self.permits = []
        self.scopes = []
        self.deny = False

    def acquire(self, scope):
        self.scopes.append(scope)
        if self.deny:
            raise RuntimeError("capacity unavailable")
        permit = FakePermit()
        self.permits.append(permit)
        return permit


class FakeQueue:
    def __init__(self, has_job=True):
        self.has_job = has_job
        self.events = []
        self.renew_allowed = True
        self.claimed = 0
        self.fence = 7

    def claim(self, scope, *, worker_id, lease_seconds):
        self.events.append(("claim", scope, worker_id))
        self.claimed += 1
        if not self.has_job:
            return None
        return LeasedTurn("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                          {"mode": "sandbox"}, self.fence, 1)

    def renew(self, scope, turn_id, *, worker_id, fence, lease_seconds):
        self.events.append(("renew", scope, fence))
        return self.renew_allowed and fence == self.fence

    def complete(self, scope, turn_id, *, worker_id, fence, result):
        self.events.append(("complete", scope, fence, result))
        return self.renew_allowed and fence == self.fence

    def fail(self, scope, turn_id, *, worker_id, fence, error_kind):
        self.events.append(("fail", scope, fence, error_kind))
        return self.renew_allowed and fence == self.fence


class Runner:
    isolated_sandbox = True

    def __init__(self, callback=None):
        self.callback = callback or (lambda _turn, _auth, _cancel: {"ok": True})
        self.called = 0

    def __call__(self, turn, authority, cancelled):
        self.called += 1
        assert authority.scope == OWNER
        assert not cancelled.is_set()
        return self.callback(turn, authority, cancelled)


def setup(runner=None, has_job=True, heartbeat_seconds=0.01):
    membership, queue, admission = FakeMembership(), FakeQueue(has_job), FakeAdmission()
    runner = runner or Runner()
    worker = HostedTurnWorker(
        membership_store=membership, queue=queue, admission=admission,
        runner=runner, lease_seconds=5, heartbeat_seconds=heartbeat_seconds,
    )
    return worker, membership, queue, admission, runner


def test_worker_success_lease_order_and_scoped_result():
    worker, membership, queue, admission, runner = setup()
    outcome = worker.run_one(identity=HostedIdentity(OWNER), worker_id="trusted-worker")
    assert outcome.status == "completed"
    assert outcome.fence == 7
    assert admission.scopes == [OWNER]
    assert admission.permits[0].released
    assert runner.called == 1
    assert [event[0] for event in queue.events] == ["claim", "renew", "renew", "complete"]
    assert all(event[1] == OWNER for event in queue.events)


def test_worker_no_job_releases_quota_without_starting_agent():
    worker, _, queue, admission, runner = setup(has_job=False)
    assert worker.run_one(identity=HostedIdentity(OWNER), worker_id="w").status == "idle"
    assert admission.permits[0].released
    assert runner.called == 0
    assert queue.claimed == 1


def test_worker_rejects_cross_tenant_before_quota_or_claim():
    worker, membership, queue, admission, runner = setup()
    with pytest.raises(HostedLeaseLost):
        worker.run_one(identity=HostedIdentity(OTHER), worker_id="w")
    assert admission.scopes == [] and queue.claimed == runner.called == 0
    assert membership.checked == [OTHER]
    membership.fail = True
    with pytest.raises(HostedLeaseLost):
        worker.run_one(identity=HostedIdentity(OWNER), worker_id="w")
    assert admission.scopes == []


def test_worker_capacity_exhaustion_leaves_durable_job_unclaimed():
    worker, _, queue, admission, runner = setup()
    admission.deny = True
    with pytest.raises(RuntimeError, match="capacity"):
        worker.run_one(identity=HostedIdentity(OWNER), worker_id="w")
    assert queue.claimed == runner.called == 0


def test_worker_revocation_after_claim_prevents_execution():
    worker, membership, queue, admission, runner = setup()
    original = queue.claim

    def revoke(*args, **kwargs):
        turn = original(*args, **kwargs)
        membership.active.clear()
        return turn

    queue.claim = revoke
    with pytest.raises(HostedLeaseLost, match="before execution"):
        worker.run_one(identity=HostedIdentity(OWNER), worker_id="w")
    assert runner.called == 0
    assert admission.permits[0].released
    assert not any(e[0] == "complete" for e in queue.events)


def test_worker_lease_loss_during_run_notifies_cancel_and_blocks_publish():
    entered = threading.Event()
    stopped = threading.Event()

    def blocking(turn, authority, cancel):
        entered.set()
        assert cancel.wait(timeout=2)
        stopped.set()
        return {"must_not_publish": True}

    worker, membership, queue, admission, runner = setup(Runner(blocking))
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(worker.run_one, identity=HostedIdentity(OWNER), worker_id="w")
        assert entered.wait(timeout=2)
        # Live membership revocation: heartbeat must stop the guest.
        membership.active.clear()
        with pytest.raises(HostedLeaseLost):
            task.result(timeout=3)
    assert stopped.is_set()
    assert admission.permits[0].released
    assert not any(e[0] == "complete" for e in queue.events)


def test_worker_post_execution_fencing_race_denies_stale_publish():
    def expire(*args):
        queue.renew_allowed = False
        return {"text": "uncommitted"}

    worker, membership, queue, admission, runner = setup(Runner(expire))
    with pytest.raises(HostedLeaseLost):
        worker.run_one(identity=HostedIdentity(OWNER), worker_id="w")
    assert not any(e[0] == "complete" for e in queue.events)
    assert admission.permits[0].released


def test_worker_exception_is_redacted_and_terminally_fenced():
    def fail(*args):
        raise RuntimeError("secret-credential-leak")

    worker, _, queue, admission, runner = setup(Runner(fail))
    outcome = worker.run_one(identity=HostedIdentity(OWNER), worker_id="w")
    assert outcome.status == "failed"
    assert queue.events[-1] == ("fail", OWNER, 7, "worker_execution_failed")
    assert admission.permits[0].released


def test_unverified_identity_or_unsafe_runner_never_accepted():
    worker, _, queue, admission, _ = setup()
    with pytest.raises(TypeError):
        worker.run_one(identity=OWNER, worker_id="w")
    with pytest.raises(ValueError):
        worker.run_one(identity=HostedIdentity(OWNER), worker_id="bad/id")
    assert queue.claimed == 0
    with pytest.raises(HostedWorkerUnavailable):
        HostedTurnWorker(
            queue=queue, admission=admission, membership_store=FakeMembership(),
            runner=lambda *a: {"ok": True},
        )
