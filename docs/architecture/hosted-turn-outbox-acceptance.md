# Hosted durable turn-state outbox — P0 acceptance boundary (2026-10-22)

This batch adds **transactional, immutable, session-local cursor replay** to the
existing PostgreSQL fenced turn ledger. It is not a live hosted LLM Agent.

## Database and security contract

1. A trusted migration role applies
   `db/migrations/0004_private_turn_event_outbox.sql` after 0001..0003.
   The runtime role gets **SELECT and INSERT only** on
   `ridge.private_turn_events` (no UPDATE/DELETE). The owner row's
   `private_sessions.turn_event_cursor` increments under a session-row lock.
2. Every successful ledger transition in `PostgresPrivateTurnQueue`
   (queued/running/completed/failed/cancelled, including lease expiration)
   allocates a cursor and inserts a state event in the **same SQL transaction**.
   Duplicate idempotent enqueue and rejected stale worker transitions emit
   nothing. A missing migration must cause an error, never silent fallback.
3. The event row contains **no user prompt, shell command, stdout, stderr,
   private artifact or model output**. Only turn UUID, state, fence and cursor.
   Expired work stays terminal failed; this ledger does not retry unknown
   external side effects.
4. `events(scope,session_id,after,limit)` is scoped by strict MemoryScope,
   session ID, forced PostgreSQL RLS and an owned-session existence check.
   Sequence IDs are local to that session and do not disclose global traffic.
   Session deletion cascades into the event rows.

## Authenticated API surface

`create_hosted_app` offers read-only, protected routes:

- `GET /api/v2/sessions/{sid}/events?after=0&limit=100`
  returns a bounded ordered event page.
- `GET /api/v2/sessions/{sid}/events/stream`
  returns a **finite SSE replay batch** (`id:`, `event: turn-state`,
  `data:`), and accepts a numeric `Last-Event-ID` header on reconnect.
  The client reconnects after each finite response; no infinite open stream
  or cross-process publish/subscribe is implied.

Both require signed OIDC and fresh database membership checks. Missing/outside
session is 404, revoked membership is 403, unavailable ledger is 503.
The service still exposes NO user-submitted shell jobs or hosted Agent runs.

## Real acceptance

- `tests/test_postgres_turn_outbox.py` requires real PostgreSQL 16 and proves
  exact idempotency, ordered concurrent cursors, cross-tenant isolation,
  terminal expiry/stale-fence suppression, immutable event rows and input
  validation.
- `tests/test_hosted_turn_events_api.py` exercises finite SSE framing,
  reconnect cursor, OIDC membership revocation, absent sessions and disabled
  event-store fail-closed behavior.
- Both PostgreSQL workflows apply migration 0004 and the exact SELECT+INSERT
  runtime grant; the unfiltered real-integration lane fails on any skip.

## Still blocked before a production Hosted Agent

This is a **durable state feed**, not yet a durable model/output stream. Absent:
authenticated enqueue and idempotent turn API, authoritative request quotas,
durable model checkpoints, continuous SSE notification/long-poll, worker-pool
startup and crash recovery with distributed cancellation, per-event retention
and purge/archive policy, cross-region failover and real 100+ tenant staging.
Only fenced Docker single-command turns have a tested isolated runner.
Production hosted Agent release remains **NO-GO**.
