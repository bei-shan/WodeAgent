# Ridge Code production acceptance gates — 2026-10-15

**Release decision: NO-GO for a public multi-tenant Agent service.** Passing unit,
API smoke, Redis, and PostgreSQL CI proves important *components*, not that
untrusted users can safely execute code through the complete hosted system.

## Which service may be deployed?

| Deployment | Scope today | Decision |
|---|---|---|
| Single-operator local Web `create_app` | Trusted loopback host; local tools can access host | Dev / local acceptance only |
| Separate hosted `create_hosted_app` | Signed OIDC, PostgreSQL RLS, private session **metadata CRUD** | Staging trial only until identity contract, operations, restore, load are validated |
| Hosted Agent execution / sandbox / MCP / plugin / memory / SSE | **Not wired end-to-end under verified multi-tenant principal** | **NO-GO** |
| Distributed turn queue (PostgreSQL) | Leases, CAS, scoped idempotency; no hosted worker executable | Infrastructure primitive only |
| Distributed admission (Redis) | Atomic quotas; no hosted execution heartbeat/cancel manager | Infrastructure primitive only |

**Do not** expose the legacy local Agent service as a hosted substitute. A
green `/health/live` proves only that a process answers HTTP. The separate
hosted `/api/health/ready` proves the DB RLS guard is present, not that all
production controls have been validated.

## Release-blocking gates (all required)

| ID | Gate | Objective acceptance evidence | Status |
|---|---|---|---|
| AUTH-01 | Production issuer, signed identity claims + database-authoritative membership revocation | Documented issuer/audience/JWKS, JWK rotation tests, tenant/project membership revocation test, role denial matrix | Runtime DB revocation contract implemented; live IdP integration and rotated-token evidence missing |
| AUTH-02 | Same verified principal on every HTTP/WS/SSE/agent/tool/trace/file/memory path | Automated cross-tenant IDOR suite against **deployed** service, plus denial of spoofed headers and rogue worker delegation | **Blocked** (hosted agent routes absent) |
| DATA-01 | Postgres RLS and per-owner transactions | Dedicated CI using non-superuser without BYPASSRLS, direct SQL/no-scope tests and CAS concurrency | Component automated (not staged) |
| DATA-02 | Transactional backups/restore and legacy session migration | Encrypted snapshots, restore drill, rollback with RPO/RTO evidence; no data loss | Missing |
| JOB-01 | Durable execution end-to-end | Worker crash/restart, fencing, cancellation, idempotent request replay, outbox exactly-once delivery semantics and SSE cursor replay; side-effect dedup plan | Queue primitive only |
| JOB-02 | Rate limits and fair scheduling | Redis quota enforcement integrated with verified scopes; lease renewal loss **stops** tools; bounded queues; max concurrency stress | Redis primitive only |
| EXEC-01 | Sandbox escape and egress resistance | Per-tenant ephemeral isolated compute, prompt-injection and tool ACL adversarial suite, no secret/host cross-tenant access | Not certified |
| MEM-01 | User-selected durable memory backend | Authenticated tenant/project/user provider preference, explicit opt-in/retention/delete; real Mem0/OpenViking/Tencent SDK compatibility and fail-closed provider tests | Partial Mem0 only |
| LOAD-01 | Load and reliability SLO in staging | Soak >=24h; 100+ simulated principals / configurable mixed workload; measured P95/P99, RSS, DB/Redis saturation, chaos recovery, quantitative error budget | Missing staging evidence |
| OPS-01 | Deployment and security operations | TLS everywhere, secret rotation, supply chain scan + pinned artifacts, alert routing, runbooks, cost limits, incident response and rollback drills | Missing |
| APPROVAL | Final human acceptance | Security/ops reviewer, staging run manifest, release SHA, Go/No-Go signoff | Pending |

## Current deterministic CI evidence

- `.github/workflows/web-mvp.yml`: local Web API, frontend, browser e2e,
  optional telemetry, Docker / MCP compatibility.
- `.github/workflows/postgres-tenant-store.yml`: real PostgreSQL RLS scope,
  optimistic CAS, idempotency conflict, lease fencing, cancel, retry exhaustion.
- `.github/workflows/redis-turn-capacity.yml`: real Redis multi-client quotas.
- `.github/workflows/agent-resource-soak.yml`: **offline fake-provider** soak,
  not mixed-tenant production capacity.
- Explicit contract tests live under `tests/test_hosted_identity_api.py`,
  `tests/test_postgres_turn_queue.py` and `tests/test_redis_turn_admission.py`.

## Staging API black-box check (metadata scope only)

After provisioning **two distinct OIDC principals from different tenants** and
a public staging HTTPS origin, run:

```bash
RIDGE_STAGING_BASE_URL="https://staging.example" \\
RIDGE_STAGING_TOKEN_A="<short-lived-token-for-tenant-A>" \\
RIDGE_STAGING_TOKEN_B="<short-lived-token-for-tenant-B>" \\
python scripts/staging_tenant_acceptance.py
```

The tool performs non-destructive readiness/route checks and creates two
temporary private sessions to verify cross-tenant read/list/write/delete
denial, spoofed headers, and stale revision CAS. It then deletes the temporary
sessions, and returns exit code 1 if any check fails. It refuses plain HTTP,
URL userinfo, and redirects, and never prints bearer tokens or session
snapshots. Token identities must be provisioned/verified by the operator; the
script cannot prove issuer tenant membership. **`PASS_METADATA_ONLY` is not
a GO decision for executing Agents.**

## Acceptance method

1. Deploy isolated hosted API using trusted IdP, DB runtime role (not its schema
   owner), `sslmode=verify-full` and isolated Redis. Never send prod data to CI.
2. Run all required workflows at the **exact release commit**. Collect signed
   results, vulnerability reports, image hashes and configuration manifest.
3. Run deployed staging cross-tenant penetration and recovery tests. Validate
   each endpoint across mismatched tenant/project/user principals and after
   membership removal; no privilege derived from request headers or agent text.
4. Simulate kill -9, model/provider timeouts, Redis outage, DB failover,
   horizontal replica restarts, memory-provider outage, and token/key rotation.
5. Measure target SLOs on load representative of real tools and LLM latency;
   set target numbers before the test and compare, without fabricating KPIs.
6. Perform restore and rollback, including sessions and durable event cursors.
7. Only then change status to GO with named owner, date, hash, metrics and
   rollback trigger. **The current release decision remains NO-GO.**

## Immediate engineering work

- Queue replay/cancellation/lease-expiry hardening is merged; do not confuse
  the durable ledger with a full hosted worker.
- Build a verified hosted Agent/sandbox pipeline without reusing local
  trust-based routes, then extend this document with actual staging evidence.

The production target is explicit, but **do not mark a milestone complete
because a plan or unit tests exist**.


## Database-authoritative membership rollout (migration 0003)

A valid JWT **never authorizes a project by itself**. The hosted API now
requires a currently enabled row in `ridge.private_project_memberships`
matching **tenant + project + user**. Runtime role has only `SELECT`; the
table has forced RLS and denies all access if transaction scope is absent.
It queries membership on every authenticated request, so a revoked member
must immediately receive HTTP 403. DB outages/missing privileges produce
HTTP 503 rather than falling back to JWT claims.

Deployment sequencing is **mandatory**: migrate with admin role; provision
memberships from your *trusted* IdP provisioning pipeline (new rows default
disabled); grant runtime role only schema USAGE and membership SELECT; inspect
RLS forced mode; only then deploy the code. Missing rows will deny access
rather than preserving the old insecure behavior. Never let public clients,
JWT scopes, LLM text or the runtime role mutate these rows.

Example administrator provisioning (substitute actual **verified** identities):

```sql
INSERT INTO ridge.private_project_memberships
    (tenant_id, project_id, user_id, enabled)
VALUES ('verified-tenant', 'verified-project', 'verified-subject', true)
ON CONFLICT (tenant_id, project_id, user_id)
DO UPDATE SET enabled=true, updated_at=now();
-- Immediate revocation, even if an older JWT has not expired:
UPDATE ridge.private_project_memberships
   SET enabled=false, updated_at=now()
 WHERE tenant_id='verified-tenant'
   AND project_id='verified-project'
   AND user_id='verified-subject';
```

Only run this through audited provisioning/admin credentials. Revoking
membership blocks **new hosted metadata requests**; it cannot stop a
remote already-running Agent, since hosted Agent execution is not enabled.
Future workers must check revocation while heartbeating and stop unsafe
operations immediately when the delegation expires.

**Go/No-Go unchanged**: production IdP lifecycle, live staging revocation,
backup/restore, actual Agent isolation and incident handling still need
independent evidence.
