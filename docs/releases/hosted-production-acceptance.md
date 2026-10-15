# Hosted production acceptance — blocker ledger (2026-10-15)

> **Decision: NOT ACCEPTED for public multi-tenant Agent execution.** 
> This is a non-negotiable release gate, not a checklist that turns green when
> unit tests pass. The hosted service currently exposes **private session
> metadata and snapshots only**; it deliberately cannot execute tools/LLM.

## What is verifiable now

| Capability | Evidence | Current scope |
|---|---|---|
| Exact principal validation | `desktop/service/hosted_identity.py`, `tests/test_hosted_identity_api.py` | OIDC JWT signed RS256/ES256, pinned issuer/audience/JWKS, exp/iat, signed tenant/project claims, token-age bound |
| Hosted route isolation | `desktop/service/hosted_app.py` | Distinct ASGI factory; no legacy local file/agent/plugin routes; local factory refuses `RIDGE_DEPLOYMENT_MODE=hosted` |
| Durable private sessions | `db/migrations/0001_tenant_private_sessions.sql`, `core/persistence/postgres_sessions.py` | PostgreSQL RLS (FORCE), optimistic revision/CAS, scoped CRUD, metadata-only listing, DB TLS verify-full on hosted startup |
| Work ledger / fencing | `db/migrations/0002_private_turn_jobs.sql`, `core/persistence/postgres_turn_queue.py` | Scoped queued/running/completed rows, idempotent submission, atomic SKIP LOCKED claim, heartbeat/lease expiry, monotonic fence, stale completion rejection. **NO hosted Agent worker connected** |
| Local overload control | `core/runtime/admission.py` and `tests/test_turn_admission.py` | Single Python process. Not a cluster-wide quota |
| Memory provider framework | `core/memory/` | Default OFF; local SQLite only. Mem0/OpenViking/TencentDB are **unavailable** unless real adapters are built and vetted |
| CI | `.github/workflows/web-mvp.yml`, `.github/workflows/postgres-tenant-store.yml` | Unit, Web, PostgreSQL+RLS and queue integration; record exact green SHA and Actions URL, not older runs |

## Hosted metadata-only mode

Run only from a trusted, TLS-terminated environment. Set
`RIDGE_DEPLOYMENT_MODE=hosted` (required policy marker for operators),
`RIDGE_OIDC_ISSUER`, `RIDGE_OIDC_AUDIENCE`, `RIDGE_OIDC_JWKS_URL`
and a `RIDGE_POSTGRES_DSN` URL containing `sslmode=verify-full` with
a configured CA trust chain. Configure the OIDC issuer to mint signed
`ridge_tenant` and `ridge_project` claims **only after authoritative
tenant/project membership verification**. User identity is the verified
`sub` claim. These are project-specific custom claims, not a universal
standard claim used by arbitrary OIDC providers.

Install `requirements-hosted.txt`; apply both PostgreSQL migrations
using a privileged **migration** role, then grant the runtime role only
USAGE on the schema and `SELECT/INSERT/UPDATE/DELETE` on the explicitly
named tables. Never run the hosted API as table owner, superuser or
BYPASSRLS. Do not reuse the migration credential.

Start the **separate factory**:

```bash
python -m uvicorn desktop.service.hosted_app:create_hosted_app \
  --factory --host 127.0.0.1 --port 8010
```

Only `/api/health/live`, `/api/health/ready` and
`/api/v2/sessions` private CRUD routes exist here. Readiness returns 503
when RLS or DB protection cannot be verified. Unauthorized access is
401, missing/out-of-scope private sessions are 404, CAS conflicts are 409.
Memory, tools, plugin execution and legacy local Web endpoints are not
exposed from this service.

Test using a real non-BYPASSRLS PostgreSQL role:

```bash
python -m pip install -r requirements.txt -r requirements-hosted.txt
RIDGE_TEST_POSTGRES_DSN='postgresql://...test-only...' \
  python -m pytest tests/test_postgres_private_sessions.py \
    tests/test_postgres_turn_queue.py tests/test_hosted_identity_api.py -q
```

In CI the database is ephemeral; its test DSN can be plaintext over
loopback **only** in the isolated runner. The hosted runtime refuses
unverified DB TLS.

## Blocking requirements before Agent SaaS / enterprise production

- [ ] **Authorized Worker**: implement a bounded, independently
  authenticated worker fleet with non-exportable tenant delegation;
  force sandbox/VM per turn, no host fallback, immutable tool permissions.
  Queue and lease fencing alone do not make external tool side effects
  idempotent. Use explicit idempotency and compensation for mutations.
- [ ] **Scope every service**: authenticated tenant/project ownership on
  session activation/fork/import/export, workspace/blob IDs, sandbox
  leases, MCP tokens, Skills/plugins, traces, event streams, metrics,
  billing, and background task callbacks. Add IDOR negative tests for
  all these APIs. Current local Agent routes MUST NOT be publicly exposed.
- [ ] **Recoverable durable events**: outbox/committed SSE cursor, dedupe
  on reconnect, worker death/restart/retry, no phantom success after
  partially executed tool writes. Persist cancellation and pause/approval
  tokens with timeouts and rejection for stale approvals.
- [ ] **Global fairness**: per-tenant/user/project fair queues and
  cross-replica permits, model TPM/RPM, sandbox CPU/memory/PID limits,
  bounded waits, overload 429/Retry-After, rate-limited logs.
- [ ] **Memory**: explicit opt-in persistent provider selection with
  signed private credentials, Mem0/OpenViking/TencentDB verified SDK
  integration, retrieval budgets, redaction, source provenance, deletes,
  cross-tenant extraction and poisoning evaluations. Provider failure
  must not silently route private content to a fallback.
- [ ] **Infrastructure/operations**: TLS ingress, egress allowlist,
  vault/rotation for tokens and model keys, audit retention/deletion,
  Postgres backup/PITR/restore drills, zero-downtime migrations and
  rollback, infrastructure-as-code, container/image SBOM + signatures.
- [ ] **Real staging acceptance**: run >=100 simultaneous turns with a
  representative safe workload, >30 min continuous mixed-model/MCP
  sessions, interrupted network/provider/OAuth refresh, process crash,
  DB failover, Redis outage (if used), sandbox orphan reconciliation.
  Record p50/p95/p99 latency, queue wait, CPU/RSS/FD, token costs, error
  rate, lease leaks, starvation/fairness and recovery time. Targets must
  be established against deployment hardware and model quotas; no
  unmeasured public QPS promise.
- [ ] **Independent security review**: penetration tests for cross-tenant
  payload/ID spoofing, malicious Skill/MCP prompts, arbitrary tool code,
  SSRF, path traversal, token replay, log data leakage and permission
  elevation. Require security sign-off before accepting.
- [ ] **Live Memory quality**: benchmark correctness, freshness,
  recall/precision, stale/conflicting memories, deletion propagation
  and cost across supported backends, using sanitized datasets.

## Acceptance record must include

Exact main commit SHA; full CI job links for that SHA; sanitized
staging load/soak reports; OIDC provider configuration review; RLS and
IDOR test output; Kubernetes/VM sandbox and network policy evidence;
PostgreSQL restoration results; production rollback plan; and signed
Security + SRE release approval. Until all gates have evidence the
release status stays **NOT ACCEPTED**. Neither green mocks nor this ADR
constitute production sign-off.
