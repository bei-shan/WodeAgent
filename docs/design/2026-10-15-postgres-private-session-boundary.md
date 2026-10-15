# PostgreSQL tenant-private session storage boundary (2026-10-15)

> **Experimental standalone adapter only.** This does not make the current
> local Web API multi-tenant or distributed. It is intentionally not mounted
> on any HTTP route until verified principals, membership rules and scoped
> access checks exist end-to-end.

## What is implemented

- Migration `db/migrations/0001_tenant_private_sessions.sql`: tenant,
  project, owner-user and 12-character session IDs in the composite
  primary key; JSONB snapshots, titles, timestamps and monotonic version.
- PostgreSQL RLS is both **enabled** and **forced**. SELECT/UPDATE/DELETE and
  INSERT checks compare all three private ownership dimensions against
  transaction-local `ridge.tenant_id`, `ridge.project_id`,
  `ridge.user_id` settings. Missing settings deny all rows.
- `PostgresPrivateSessionStore` sets those settings **in every transaction**,
  uses parameterized queries, and implements create/get/save/delete. A stale
  or inaccessible save fails with `SessionVersionConflict`.
- Optimistic concurrency control uses `UPDATE ... WHERE version = ? RETURNING`
  to ensure a concurrent writer cannot silently overwrite a newer snapshot.
- The optional `requirements-postgres.txt` contains psycopg. The default
  local installation does not need PostgreSQL.
- Dedicated GitHub Actions starts a real PostgreSQL service, uses a separate
  non-superuser non-`BYPASSRLS` role, and runs isolation and concurrent CAS
  tests. SQLite or administrator-backed tests cannot validate PostgreSQL RLS.

## Critical security limitations

1. A `MemoryScope` value is only a data object. It is **not proof of auth**.
   Only a trusted OIDC token verifier + membership/authorization layer may
   construct scopes from inbound requests. Never use `X-Tenant-ID` or a
   payload `tenant_id` to select a scope.
2. Custom `set_config` RLS parameters form a defense-in-depth boundary,
   **not a standalone authorization mechanism** against an attacker who can
   execute arbitrary SQL as the application role. The runtime must prohibit
   untrusted SQL execution and enforce the same ACL at the service layer.
3. Runtime DB roles cannot be superusers or have `BYPASSRLS`; ideally cannot
   own schema objects. Admin migrations and application data connections use
   different credentials. Connection-pool checkout **must** start a new
   transaction and set scope before every access.
4. The table is **private only**. A different user in the same tenant cannot
   read it even if they know the session ID. Team-shared sessions need a
   dedicated membership/ACL design, not a broader RLS bypass.
5. In-memory `SessionController`, JSON `SessionManager`, legacy tools,
   workspaces, secrets, traces, plugins, WebSockets and SSE still have no
   verified tenant context. Installing this adapter must not expose any of
   those routes to untrusted multi-user clients.
6. Existing `memory/sessions` data is not migrated. Migration must use a
   backup, typed compatibility envelope, dual-read verification and rollback.
   Never silently drop or replace users' local sessions.

## Why this is a separate milestone

The current Agent uses several coupled persistence paths:
`SessionManager`, `SessionFeature`, `SessionController`, turn outcome
files and the Web `SessionService`. Changing only one to PostgreSQL would
create split-brain, and changing all before resource-level authorization could
create cross-tenant IDOR. This PR establishes a tested, fail-closed *storage*
contract without activating shared sessions prematurely.

## Next integration steps

1. OIDC token signature, issuer, audience, lifetime and key-rotation
   verification, server-derived principal, membership lookup, request-scoped
   `tenant/project/user` authorization.
2. Backend-agnostic session catalog port, all callers migrated to that port,
   schema/version migration for existing JSON snapshots, DB session locking
   and idempotent turn ownership.
3. Every HTTP/WebSocket/SSE, export/import/fork, workspace, sandbox, MCP,
   credentials, traces and Memory route must authorize the same principal.
4. Durable task queues and event outbox + globally fenced worker leases,
   separate from this table. Redis may accelerate distributed rate-limits
   but PostgreSQL remains the authoritative task/event state.
5. Separate staging deployment, multi-tenant IDOR penetration tests, worker
   restart and transactional conflict load tests before public exposure.

Local validation with a **non-admin** PostgreSQL role:

```bash
python -m pip install -r requirements-postgres.txt pytest
RIDGE_TEST_POSTGRES_DSN='postgresql://<test_user>:<password>@localhost:5432/<db>' \
  python -m pytest tests/test_postgres_private_sessions.py -q
```

Apply the SQL migration as an admin first, then grant only
`USAGE` on schema and `SELECT/INSERT/UPDATE/DELETE` on the private session
table to the test role. See `.github/workflows/postgres-tenant-store.yml`
for an isolated automated example.
