# Signed Docker sandbox leases and bounded orphan reconciliation (2026-10-15)

## Goal

Prevent an unresponsive Docker daemon or crashed Agent process from leaving
unmanaged sandbox processes indefinitely. This extends the opt-in Docker
**Bash-only** boundary; it does not make the host application, plugins, MCP,
or the read-write workspace mount a hardened multi-tenant sandbox.

Inspired by DeerFlow's explicit sandbox provider ownership and shutdown
model: [DeerFlow sandbox](https://github.com/bytedance/deer-flow/tree/main/backend/packages/harness/deerflow/sandbox).
Ridge Code continues to launch ephemeral `docker run --rm` containers rather
than adopting a warm pool.

## Lease authenticity and expiry

The provider issues a random `wodeagent-<20 hex>` name and Docker labels for
the protocol version, installation owner fingerprint, Unix expiry, and an
HMAC-SHA256 signature over version/owner/name/expiry. The key is a persistent
random 32-byte file in
`~/.mycodeagent/sandbox-owners/<installation-root-hash>.key` outside the
container's intended disposable workspace, with POSIX 0700 parent and 0600
file checks. Never copy this key into a project workspace or a container.
A configured workspace encompassing the key path is refused at initialization.

Every lease expires at `start + tool_timeout + 180s + 1s`; tool timeout is
capped at 600 seconds. The extra margin protects healthy tools and cancellation
cleanup. The local reaper tracks active names and never removes its in-process
active tool executions. Other live processes using the same key rely on the
deadline margin, not a cross-process distributed lease protocol.

Labels alone are insufficient: before removal the reaper inspects the
container's actual name and labels, recomputes and compares the HMAC, checks
expiry and local active status, and deletes by **container ID**, never by name.
Forged labels, foreign owner identities, invalid IDs and malformed signatures
are skipped. Docker daemon administrators are outside this boundary: anyone
with privileged Docker API access already controls containers and could copy
or modify them.

## Recovery cadence and budgets

One shared daemon thread per source installation runs reconciliation on first
sandbox initialization, then every 60 seconds while the Agent process stays
alive. The reaper scans only the 24 most recently created matching labeled
containers each cycle. Each cycle has an 8-second wall-clock budget, and
individual Docker CLI calls are bounded at 2 seconds. Failures increment
sanitized counters; no names, secrets, container environment or command bodies
are logged. The scanner does not automatically retry host shell tools after a
Docker failure.

The scan cap is a deliberate resource bound. If more than 24 relevant running
containers persist and a subset never becomes eligible, older orphans may not
be reached; a full distributed scheduler or paginated lifecycle journal is
still outside the local v0.1 scope. Existing `--rm` cleanup and explicit stop
remain the first line of defense.

## Operational recovery

1. Verify Docker daemon access using the trusted operator's shell, not an
   Agent tool. The reaper never runs a command in a host shell.
2. Review the sanitized `Docker sandbox cleanup inspected/removed/skipped/failed`
   metrics. A nonzero failure count does **not** grant permission to delete
   arbitrary containers.
3. Inspect the installation's labeled containers with Docker's own
   `docker ps -a --filter label=io.ridgecode.sandbox.version=1` (operator only).
4. For expired containers belonging to this installation, initialize the
   sandbox provider or restart the local service to trigger reconciliation.
   Unrecognized/malformed labels require manual assessment; do not glob-delete
   all `wodeagent-*` containers.
5. If the owner key is lost or rotated, old signed leases cannot be verified
   and are deliberately not auto-removed. Operators must assess old containers
   manually. Key backups are sensitive.

## Verification

```bash
python -m pytest tests/test_sandbox_leases.py tests/test_sandbox_cancellation.py tests/test_sandbox_boundary.py -q
AGENT_DOCKER_INTEGRATION=1 python -m pytest tests/test_sandbox_docker_integration.py -q
```

The first suite checks concurrent leases, signature spoofing, active-work
protection, invalid/faulty Docker results, bounded scanning, restart recovery,
private key permissions and workspace/key containment. The live Docker suite
requires a trusted runner and preloaded `python:3.12-alpine` image. Mock tests
alone do not establish real daemon containment.

## Remaining failure boundary

Unexpected system suspension beyond the expiry margin may allow another
process's reaper to terminate a still-running old container. Multi-instance,
cross-host or untrusted Docker daemon deployments need a true shared ownership
store, fencing tokens and lease renewal, as well as per-tenant sandbox policy.
The key's private mode prevents accidental leakage, not attacks from the
same-UID trusted operator or a Docker administrator. No claimed protection
against malicious Python plugins, writable workspace contents or daemon bugs.
