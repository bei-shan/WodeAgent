"""Transactional, RLS-scoped durable turn ledger and fenced worker leases.

Infrastructure primitive ONLY. Local Agent execution and hosted API must
not execute from this queue until sandbox, delegated scopes and callbacks
have been independently verified. Expired executions are TERMINAL failures:
the queue never automatically replays potentially non-idempotent effects.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from core.memory.contracts import MemoryScope
from .postgres_sessions import PostgresPrivateSessionStore


_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class LeasedTurn:
    id: str
    payload: dict[str, Any]
    fence: int
    attempt: int


class TurnIdempotencyConflict(RuntimeError):
    """A replay key was reused for a different session or payload."""


class PostgresPrivateTurnQueue(PostgresPrivateSessionStore):
    def enqueue(
        self, scope: MemoryScope, session_id: str, *,
        idempotency_key: str, payload: Mapping[str, Any],
    ) -> str:
        self._validate(scope, session_id)
        if not isinstance(idempotency_key, str) or not _OWNER.fullmatch(idempotency_key):
            raise ValueError("A bounded idempotency key is required")
        if not isinstance(payload, Mapping):
            raise TypeError("Turn payload must be a JSON object")
        encoded = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False)
        if len(encoded) > 65536:
            raise ValueError("Turn payload exceeds 64 KB")
        turn_id = str(uuid.uuid4())
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                # Never infer ownership from the submitted session_id alone;
                # the composite FK and RLS protect every dimension.
                cur.execute(
                    """INSERT INTO ridge.private_turn_jobs
                         (tenant_id, project_id, owner_user_id, turn_id,
                          session_id, idempotency_key, payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (tenant_id, project_id, owner_user_id, idempotency_key)
                       DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key
                         WHERE ridge.private_turn_jobs.session_id=EXCLUDED.session_id
                           AND ridge.private_turn_jobs.payload=EXCLUDED.payload
                       RETURNING turn_id""",
                    (scope.tenant_id, scope.project_id, scope.user_id,
                     turn_id, session_id, idempotency_key, encoded),
                )
                row = cur.fetchone()
                if row is None:
                    # A duplicate key with different contents must never
                    # silently alias an unrelated tool/agent execution.
                    # The same-key same-payload retry still returns its old ID.
                    raise TurnIdempotencyConflict(
                        "Turn idempotency key is already used for different work"
                    )
                return str(row[0])

    def claim(
        self, scope: MemoryScope, *, worker_id: str, lease_seconds: int = 30,
    ) -> LeasedTurn | None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Verified worker delegation scope required")
        if not isinstance(worker_id, str) or not _OWNER.fullmatch(worker_id):
            raise ValueError("Invalid worker identity")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be 1..3600")
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                # A crashed worker may have performed irreversible effects.
                # The DB fence prevents stale publication but does not undo
                # Shell, filesystem or network changes: NEVER auto-replay.
                self._reap_expired(cur, scope)
                cur.execute(
                    """WITH picked AS (
                        SELECT turn_id FROM ridge.private_turn_jobs
                        WHERE tenant_id=%s AND project_id=%s AND owner_user_id=%s
                          AND status='queued'
                        ORDER BY created_at, turn_id
                        FOR UPDATE SKIP LOCKED LIMIT 1
                       )
                       UPDATE ridge.private_turn_jobs j
                       SET status='running', fence=j.fence+1,
                           attempts=j.attempts+1, lease_owner=%s,
                           lease_until=now()+(%s * interval '1 second'),
                           updated_at=now()
                       FROM picked p
                       WHERE j.turn_id=p.turn_id
                         AND j.tenant_id=%s AND j.project_id=%s
                         AND j.owner_user_id=%s
                       RETURNING j.turn_id, j.payload, j.fence, j.attempts""",
                    (scope.tenant_id, scope.project_id, scope.user_id,
                     worker_id, lease_seconds,
                     scope.tenant_id, scope.project_id, scope.user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                payload = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                return LeasedTurn(str(row[0]), payload, int(row[2]), int(row[3]))


    @staticmethod
    def _reap_expired(cur, scope: MemoryScope, *, limit: int = 100) -> int:
        """Mark uncertain executions as terminal failures, never replay.

        Expired workers may already have changed external state. Releasing a
        stale PostgreSQL lease is NOT evidence of idempotency. Each failure
        bumps the DB fencing token, redacts the original payload, and prevents
        a second worker from claiming it automatically. A new user request
        must enqueue a new job after observing the uncertainty.
        """
        cur.execute(
            """WITH expired AS (
                 SELECT turn_id FROM ridge.private_turn_jobs
                  WHERE tenant_id=%s AND project_id=%s AND owner_user_id=%s
                    AND status='running' AND lease_until <= now()
                  ORDER BY lease_until, turn_id
                  FOR UPDATE SKIP LOCKED LIMIT %s
               )
               UPDATE ridge.private_turn_jobs j
                  SET status='failed',
                      result='{"error_kind":"lease_expired_uncertain"}'::jsonb,
                      lease_owner=NULL, lease_until=NULL,
                      fence=j.fence+1, updated_at=now()
                 FROM expired e
                WHERE j.turn_id=e.turn_id
                  AND j.tenant_id=%s AND j.project_id=%s
                  AND j.owner_user_id=%s""",
            (scope.tenant_id, scope.project_id, scope.user_id, limit,
             scope.tenant_id, scope.project_id, scope.user_id),
        )
        return cur.rowcount

    def reap_expired(self, scope: MemoryScope, *, limit: int = 100) -> int:
        """Bounded, tenant-scoped cleanup of unconfirmed expired turns."""
        if not isinstance(scope, MemoryScope):
            raise TypeError("Verified worker delegation scope required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Sweep batch size must be between 1 and 1000")
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                return self._reap_expired(cur, scope, limit=limit)

    def cancel(self, scope: MemoryScope, turn_id: str) -> bool:
        """Terminally revoke a queued or executing job.

        Bump the fencing token atomically so an in-flight stale worker can
        never renew or publish success through this ledger. This does NOT
        forcibly undo or stop external side effects; worker cancellation
        propagation is a separate requirement.
        """
        if not isinstance(scope, MemoryScope):
            raise TypeError("Server-verified session scope required")
        if not isinstance(turn_id, str):
            raise ValueError("Invalid turn ID")
        try:
            parsed = uuid.UUID(turn_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid turn ID") from exc
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """UPDATE ridge.private_turn_jobs
                          SET status='cancelled', fence=fence+1,
                              lease_owner=NULL, lease_until=NULL,
                              updated_at=now()
                        WHERE tenant_id=%s AND project_id=%s
                          AND owner_user_id=%s AND turn_id=%s
                          AND status IN ('queued', 'running')""",
                    (scope.tenant_id, scope.project_id, scope.user_id, str(parsed)),
                )
                return cur.rowcount == 1

    def renew(
        self, scope: MemoryScope, turn_id: str, *,
        worker_id: str, fence: int, lease_seconds: int = 30,
    ) -> bool:
        return self._lease_update(scope, turn_id, worker_id, fence,
                                  "renew", seconds=lease_seconds)

    def complete(
        self, scope: MemoryScope, turn_id: str, *,
        worker_id: str, fence: int, result: Mapping[str, Any],
    ) -> bool:
        if not isinstance(result, Mapping):
            raise TypeError("Result must be a JSON object")
        encoded = json.dumps(dict(result), ensure_ascii=False, allow_nan=False)
        if len(encoded) > 65536:
            raise ValueError("Turn result exceeds 64 KB")
        return self._lease_update(scope, turn_id, worker_id, fence, "complete",
                                  result=encoded)

    def fail(
        self, scope: MemoryScope, turn_id: str, *, worker_id: str, fence: int,
        error_kind: str = "worker_execution_failed",
    ) -> bool:
        """Terminally fail a job under the CURRENT lease; never expose secrets."""
        if error_kind not in {"worker_execution_failed", "invalid_payload"}:
            raise ValueError("Invalid bounded failure kind")
        return self._lease_update(
            scope, turn_id, worker_id, fence, "fail",
            result=json.dumps({"error_kind": error_kind}),
        )

    def _lease_update(
        self, scope: MemoryScope, turn_id: str, worker_id: str, fence: int,
        action: str, *, seconds: int = 30, result: str | None = None,
    ) -> bool:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Verified worker delegation scope required")
        if not isinstance(worker_id, str) or not _OWNER.fullmatch(worker_id):
            raise ValueError("Invalid worker identity")
        if type(fence) is not int or fence < 1:
            raise ValueError("Invalid fencing token")
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise ValueError("Invalid lease duration")
        if action not in {"renew", "complete", "fail"}:
            raise ValueError("Invalid lease operation")
        if action == "renew":
            update = "lease_until=now()+(%s * interval '1 second'), updated_at=now()"
            value = seconds
        else:
            # SQL fragment derived from the fixed internal action, never user input.
            status = "completed" if action == "complete" else "failed"
            update = (
                f"status='{status}', result=%s::jsonb, lease_owner=NULL, "
                "lease_until=NULL, updated_at=now()"
            )
            value = result
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    f"""UPDATE ridge.private_turn_jobs SET {update}
                        WHERE tenant_id=%s AND project_id=%s AND owner_user_id=%s
                          AND turn_id=%s AND lease_owner=%s AND fence=%s
                          AND status='running' AND lease_until > now()""",
                    (value, scope.tenant_id, scope.project_id, scope.user_id,
                     turn_id, worker_id, fence),
                )
                return cur.rowcount == 1
