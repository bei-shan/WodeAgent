# Hosted Docker command-turn adapter — actual scope and release gates

`core/runtime/hosted_docker_runner.py` implements `DockerHostedTurnRunner` for
`HostedTurnWorker`. It executes an authenticated, Postgres-claimed, Redis-admitted
**one-shot command job**, with a fresh empty ephemeral workspace, Docker
`--network=none`, no guest Docker socket, no project mount, no inherited
cloud/API keys, bounded outputs and cooperative cancellation. The worker
rechecks verified project membership and the Postgres/Redis fence before
committing output, and deliberately never retries uncertain shell side effects.

Only an exact payload object is accepted:

`{"kind":"docker-command","command":"echo ready","timeout_seconds":20}`

The command is run in the guest shell; the caller cannot configure mounts,
backend, image, network, environment or output destination. Any one of those
inputs appearing in the job payload fails closed. A production API must enforce
authenticated enqueue, allowed user actions, resource quotas and audit before
using this adapter for tenants.

This is **NOT a full hosted LLM Agent**: there is no durable LLM state snapshot,
SSE outbox result cursor, authenticated session turn API, arbitrary file export
or cross-process Agent tool recovery in this batch.

## Tests

- `tests/test_hosted_docker_runner.py`: strict job schema, fake identity,
  cancellation, membership revocation, ephemeral owner isolation.
- `tests/test_hosted_worker.py`: Postgres/Redis lease coordination, stale
  result fencing and fail-closed membership tests (existing).
- `tests/test_hosted_docker_real.py`: actual Docker guest with no host
  workspace, no Docker socket and no outgoing network. Required by the
  always-on Docker real-integration CI (zero skips accepted).

## Remaining multi-tenant release blockers

- Tie OIDC/hosted session row to an Agent turn endpoint and enforce
  idempotency, result publication outbox and resumable SSE cursor.
- Scope artifact storage by verified membership and session ownership.
- Run real PostgreSQL+Redis+Docker workers concurrently, crash and restart
  processes, revoke membership mid-command, verify no stale output/retry.
- Guarantee task queue cancellation is propagated to running Docker workers
  under cross-host cancellation and worker death (not just in-process Event).
- Exercise real hostile-tenant policy, browser/computer access prohibition,
  resource quotas and network egress audit on staging; 24h scale and
  operations sign-off remain **BLOCKED**.

Until then public multi-tenant Agent execution is **NO-GO**.
