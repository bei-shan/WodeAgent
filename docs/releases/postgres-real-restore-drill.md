# P2 PostgreSQL backup/restore acceptance (CI-only, 2026-10-22)

## What this actually proves

The mandatory `PostgreSQL RLS and durable queue` job in
`.github/workflows/real-integration-gates.yml` now performs a **real**
`pg_dump -Fc` → new database → `pg_restore --single-transaction` round
trip after its PostgreSQL+RLS+queue+outbox integration suite.

The tool `scripts/postgres_backup_restore_acceptance.py` seeds two independent
tenant owners sharing a session ID, records a fenced completed job and its
three immutable events, takes a backup, changes the source *after* backup,
restores into a **new isolated database**, and verifies:

- Verified runtime Postgres role still has RLS and immutability grants after restore.
- Both owners' separate session contents survive; a third owner sees neither.
- The captured owner event state and per-session cursors match the backup
  exactly; work created *after* backup must not appear.
- Reusing the original idempotency key does not replay command side effects
  or insert a duplicate event, and fresh events continue with the next cursor.
- The test database is dropped even on failure, and no backup bytes are
  uploaded or retained as public CI artifacts.

The runner rejects all DSNs except `127.0.0.1:5432/ridge_ci`, requires
`CI=true`, separate runtime/admin users, and generates its own restore
database name. **Never point this test at production.**

## Still NOT covered by this P2 slice

A dump/restore smoke test is not PostgreSQL PITR or high availability:
there is no WAL replay, restore onto independent hosts, tested storage
encryption/key recovery, backup/object-store lifecycle, RPO/RTO SLA or
cross-region failover. Operational release remains **NO-GO** pending
actual staging exercises, 24-hour durability soak, at least 100 simultaneous
tenant turns, verified restore under load, independent security review,
repeatable runbooks and signed release authorization.
