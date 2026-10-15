# Distributed Agent turn admission (Redis prototype)

> **Infrastructure primitive only**. It is not attached to the local API or
> hosted Agent workers. No assertion of complete multi-tenant production
> readiness is made by passing its tests.

`core/runtime/distributed_admission.py` provides an atomic multi-instance
permit manager. `RedisTurnAdmission` uses Redis Lua EVAL and server time to
check four quota levels **in one atomic operation**: global, tenant, user,
project. An HMAC of a verified `MemoryScope` generates opaque redis keys.
All keys share one Redis Cluster hash tag, so the atomic script stays on
one shard (a deliberate scalability tradeoff).

Each permit has an unguessable token and a bounded TTL. Workers **must**
call `permit.renew()` before expiry; failure requires immediately
canceling/pause of the task. Terminated workers eventually release quota
through lease expiry; duplicate `release()` is harmless. Redis outages
raise `DistributedAdmissionUnavailable` and **never** fall back to
process-local admission.

The Redis client must be provisioned by the operator with authenticated
`rediss://` transport, network allowlists and capacity planning.
`RIDGE_TEST_REDIS_URL=redis://...` is for disposable loopback CI only.
Never derive tenants from untrusted HTTP headers or model/tool arguments.
The HMAC secret must come from a vault; preserve its value across worker
restarts and implement coordinated migration for key rotation.

### Not implemented or verified here

- No global **queue fairness** or durable job scheduler, despite bounded
  leases. The PostgreSQL private turn ledger remains authoritative.
- No hosted Worker integration; no post-crash safe tool or LLM replay.
- No central operational metrics, lease reconciliation controller or
  tracing of renew-loss cancellation.
- No Redis failover/partition chaos, multi-region cluster or 24h soak test.
- Redis Lua script TTL revocation prevents *future result publication* only
  when the executor checks its permit. It cannot roll back arbitrary network
  calls or shell commands that already ran.
- A single hash tag centralizes all deployment keys on one shard; large
  throughput requires capacity measurements and potentially sharded
  admission hierarchy design.

Real Redis integration is exercised in
`tests/test_redis_turn_admission.py` and
`.github/workflows/redis-turn-capacity.yml`. Tests cover cross-client
atomicity, 60 competing admissions, tenant caps, renewal, lease expiry,
key privacy, and Redis-down fail-closed behavior.

Do not enable for real users until the hosted worker also enforces
OIDC/ACL scopes, durable turn fencing and cancellation, sandbox isolation,
and audited per-tenant resource quotas. Full prerequisites:
`docs/releases/hosted-production-acceptance.md`.
