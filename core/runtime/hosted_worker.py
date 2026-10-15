"""Fenced, one-turn hosted worker coordinator (no HTTP routes).

Composes already-isolated PostgreSQL turn claims and Redis admission leases.
Only a trusted application adapter can supply a verified identity, durable
stores and a runner that accepts cooperative cancellation.

This infrastructure does not make arbitrary shell/LLM side effects idempotent.
Callers must use an isolated sandbox and must not replay non-idempotent work.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from core.persistence.postgres_turn_queue import LeasedTurn
from core.runtime.tool_authorization import (
    HostedToolAuthority, trusted_hosted_tool_authority,
)
from desktop.service.hosted_identity import HostedIdentity

log = logging.getLogger(__name__)
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class HostedLeaseLost(RuntimeError):
    """Cancellation, membership revocation, or a distributed fencing failure."""


class HostedWorkerUnavailable(RuntimeError):
    """A worker prerequisite is absent or not ready."""


class IsolatedTurnRunner(Protocol):
    """A trusted sandbox-backed adapter; cancellation must stop its guest work."""
    isolated_sandbox: bool

    def __call__(
        self, turn: LeasedTurn, authority: HostedToolAuthority,
        cancelled: threading.Event,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HostedTurnOutcome:
    status: str
    turn_id: str | None = None
    fence: int | None = None


class HostedTurnWorker:
    """Execute one private job only while Redis + PostgreSQL leases are live.

    The caller may not specify a MemoryScope or tenant id: identity comes from
    the trusted OIDC verifier. A dedicated adapter must acquire its own fresh
    isolated sandbox and fence every external publication. A cancelled event
    must stop guest work; this coordinator cannot undo completed side effects.
    """

    def __init__(
        self, *, queue: Any, admission: Any, membership_store: Any,
        runner: IsolatedTurnRunner, lease_seconds: int = 30,
        heartbeat_seconds: float = 5.0,
    ):
        if not getattr(runner, "isolated_sandbox", False) is True:
            raise HostedWorkerUnavailable("A trusted isolated sandbox runner is required")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 3600:
            raise ValueError("Worker lease must be 5..3600 seconds")
        if type(heartbeat_seconds) not in (int, float) or not 0 < heartbeat_seconds < lease_seconds / 2:
            raise ValueError("Worker heartbeat must be less than half the lease")
        if not callable(getattr(membership_store, "authorize_membership", None)):
            raise HostedWorkerUnavailable("Authoritative membership store required")
        for name, target, methods in (
            ("queue", queue, ("claim", "renew", "complete", "fail")),
            ("admission", admission, ("acquire",)),
        ):
            if any(not callable(getattr(target, method, None)) for method in methods):
                raise HostedWorkerUnavailable(f"Invalid hosted {name} interface")
        self.queue = queue
        self.admission = admission
        self.membership_store = membership_store
        self.runner = runner
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = float(heartbeat_seconds)

    def run_one(self, *, identity: HostedIdentity, worker_id: str) -> HostedTurnOutcome:
        if type(identity) is not HostedIdentity:
            raise TypeError("Verified OIDC HostedIdentity required")
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("Invalid worker id")

        authority = trusted_hosted_tool_authority(identity, self.membership_store)
        if not authority.allows("Bash"):
            raise HostedLeaseLost("Project membership is unavailable or revoked")

        # Admission comes FIRST: a full tenant cannot claim a queue job and
        # occupy a durable lease while waiting for compute capacity.
        permit = self.admission.acquire(identity.scope)
        closed = threading.Event()
        cancelled = threading.Event()
        lost = threading.Event()
        monitor: threading.Thread | None = None
        lease: LeasedTurn | None = None

        def renew_once() -> bool:
            try:
                if not authority.allows("Bash"):
                    return False
                if permit.renew() is not True:
                    return False
                if self.queue.renew(
                    identity.scope, lease.id, worker_id=worker_id,
                    fence=lease.fence, lease_seconds=self.lease_seconds,
                ) is not True:
                    return False
                return True
            except Exception:
                log.warning("Hosted worker lease verification failed; cancelling")
                return False

        def watch() -> None:
            while not closed.wait(self.heartbeat_seconds):
                if not renew_once():
                    lost.set()
                    cancelled.set()
                    return

        try:
            lease = self.queue.claim(
                identity.scope, worker_id=worker_id, lease_seconds=self.lease_seconds,
            )
            if lease is None:
                return HostedTurnOutcome("idle")
            if not isinstance(lease, LeasedTurn):
                raise HostedWorkerUnavailable("Invalid fenced job from durable queue")
            # Re-check after DB claim, before any model or sandbox work.
            if not renew_once():
                raise HostedLeaseLost("Lease lost before execution")
            monitor = threading.Thread(
                target=watch, name="hosted-turn-lease-watch", daemon=True,
            )
            monitor.start()

            try:
                output = self.runner(lease, authority, cancelled)
                if not isinstance(output, Mapping):
                    raise ValueError("Isolated runner must return a result object")
                # Reverify directly before publication; a stale/expired
                # PostgreSQL fence cannot commit a result.
                if lost.is_set() or cancelled.is_set() or not renew_once():
                    raise HostedLeaseLost("Lease lost before result publication")
                if not self.queue.complete(
                    identity.scope, lease.id, worker_id=worker_id,
                    fence=lease.fence, result=dict(output),
                ):
                    raise HostedLeaseLost("PostgreSQL rejected stale worker result")
                return HostedTurnOutcome("completed", lease.id, lease.fence)
            except HostedLeaseLost:
                cancelled.set()
                raise
            except Exception:
                # Never expose exception text or payload to user-facing
                # status. A fenced terminal failure prevents unsafe replay.
                cancelled.set()
                if lost.is_set() or not renew_once():
                    raise HostedLeaseLost("Worker lease lost during failed turn") from None
                if not self.queue.fail(
                    identity.scope, lease.id, worker_id=worker_id,
                    fence=lease.fence, error_kind="worker_execution_failed",
                ):
                    raise HostedLeaseLost("PostgreSQL rejected failed result") from None
                return HostedTurnOutcome("failed", lease.id, lease.fence)
        finally:
            closed.set()
            if monitor is not None:
                monitor.join(timeout=self.heartbeat_seconds + 1)
            # Never silently fall back to local quotas after Redis failure.
            # Permit tokens have bounded TTL if release itself fails.
            try:
                permit.release()
            except Exception:
                log.warning("Hosted admission release failed; lease will expire")
