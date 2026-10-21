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
    def event_security_probe(self) -> bool:
        """Readiness MUST reject missing RLS, permissive role or missing table."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.relrowsecurity, c.relforcerowsecurity,
                              EXISTS (SELECT 1 FROM pg_policies p
                                WHERE p.schemaname='ridge'
                                  AND p.tablename='private_turn_events'),
                              has_table_privilege(
                                current_user, 'ridge.private_turn_events', 'SELECT'
                              ),
                              has_table_privilege(
                                current_user, 'ridge.private_turn_events', 'INSERT'
                              ),
                              has_table_privilege(
                                current_user, 'ridge.private_turn_events', 'UPDATE'
                              ),
                              has_table_privilege(
                                current_user, 'ridge.private_turn_events', 'DELETE'
                              ),
                              r.rolsuper, r.rolbypassrls
                       FROM pg_class c
                       JOIN pg_namespace n ON n.oid=c.relnamespace
                       JOIN pg_roles r ON r.rolname=current_user
                       WHERE n.nspname='ridge'
                         AND c.relname='private_turn_events'"""
                )
                row = cur.fetchone()
                return bool(row and row[:5] == (True, True, True, True, True)
                            and row[5:] == (False, False, False, False))

    @staticmethod
    def _event(cur, scope: MemoryScope, session_id: str, turn_id: str,
               state: str, fence: int) -> None:
        """Reserve owner-local cursor, then insert the immutable event atomically.

        The update locks the owning session row so concurrent workers cannot
        allocate duplicate/out-of-order cursors. NO result or payload is
        published here. RLS and the composite session FK are mandatory.
        """
        if state not in {"queued", "running", "completed", "failed", "cancelled"}:
            raise ValueError("Invalid internal turn event state")
        cur.execute(
            """UPDATE ridge.private_sessions
                  SET turn_event_cursor=turn_event_cursor+1
                WHERE tenant_id=%s AND project_id=%s AND owner_user_id=%s
                  AND session_id=%s
                RETURNING turn_event_cursor""",
            (scope.tenant_id, scope.project_id, scope.user_id, session_id),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Cannot allocate event cursor outside owned session")
        cur.execute(
            """INSERT INTO ridge.private_turn_events
                 (tenant_id, project_id, owner_user_id, session_id,
                  event_seq, turn_id, state, fence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (scope.tenant_id, scope.project_id, scope.user_id,
             session_id, int(row[0]), turn_id, state, fence),
        )

    def events(
        self, scope: MemoryScope, session_id: str, *,
        after: int = 0, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Finite, read-only replay after an owner-local cursor.

        Missing or out-of-scope sessions return LookupError, not metadata
        about their events. The authenticated API must recheck membership.
        """
        self._validate(scope, session_id)
        if type(after) is not int or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid event cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Event batch limit must be 1..100")
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._scope(cur, scope)
                cur.execute(
                    """SELECT 1 FROM ridge.private_sessions
                         WHERE tenant_id=%s AND project_id=%s
                           AND owner_user_id=%s AND session_id=%s""",
                    (scope.tenant_id, scope.project_id, scope.user_id, session_id),
                )
                if cur.fetchone() is None:
                    raise LookupError("Owned session does not exist")
                cur.execute(
                    """SELECT event_seq, turn_id, state, fence
                         FROM ridge.private_turn_events
                        WHERE tenant_id=%s AND project_id=%s
                          AND owner_user_id=%s AND session_id=%s
                          AND event_seq>%s
                        ORDER BY event_seq LIMIT %s""",
                    (scope.tenant_id, scope.project_id, scope.user_id,
                     session_id, after, limit),
                )
                return [
                    {"id": int(seq), "turn_id": str(tid),
                     "state": state, "fence": int(fence)}
                    for seq, tid, state, fence in cur.fetchall()
                ]

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
                       RETURNING turn_id, session_id, fence""",
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
                # The conflict-update locks the ledger row. A retried
                # idempotency key may not append a second queued event.
                cur.execute(
                    """SELECT 1 FROM ridge.private_turn_events
                         WHERE tenant_id=%s AND project_id=%s
                           AND owner_user_id=%s AND turn_id=%s
                         LIMIT 1""",
                    (scope.tenant_id, scope.project_id, scope.user_id, row[0]),
                )
                if cur.fetchone() is None:
                    self._event(cur, scope, str(row[1]), str(row[0]), "queued", int(row[2]))
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
                cur.execute(
                    """SELECT session_id FROM ridge.private_turn_jobs
                         WHERE tenant_id=%s AND project_id=%s
                           AND owner_user_id=%s AND turn_id=%s""",
                    (scope.tenant_id, scope.project_id, scope.user_id, row[0]),
                )
                owned_session = cur.fetchone()
                if owned_session is None:
                    raise RuntimeError("Claimed turn has no owned session")
                self._event(cur, scope, str(owned_session[0]), str(row[0]), "running", int(row[2]))
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
                  AND j.owner_user_id=%s
               RETURNING j.turn_id, j.session_id, j.fence""",
            (scope.tenant_id, scope.project_id, scope.user_id, limit,
             scope.tenant_id, scope.project_id, scope.user_id),
        )
        rows = cur.fetchall()
        for turn_id, session_id, fence in rows:
            PostgresPrivateTurnQueue._event(
                cur, scope, str(session_id), str(turn_id), "failed", int(fence),
            )
        return len(rows)

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
                          AND status IN ('queued', 'running')
                        RETURNING session_id, fence""",
                    (scope.tenant_id, scope.project_id, scope.user_id, str(parsed)),
                )
                row = cur.fetchone()
                if row is not None:
                    self._event(cur, scope, str(row[0]), str(parsed), "cancelled", int(row[1]))
                return row is not None

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
                          AND status='running' AND lease_until > now()
                        RETURNING session_id, fence""",
                    (value, scope.tenant_id, scope.project_id, scope.user_id,
                     turn_id, worker_id, fence),
                )
                row = cur.fetchone()
                if row is not None and action != "renew":
                    self._event(
                        cur, scope, str(row[0]), turn_id,
                        "completed" if action == "complete" else "failed",
                        int(row[1]),
                    )
                return row is not None
