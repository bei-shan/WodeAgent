# Experimental E2B cloud micro-VM backend — single-gateway phase (2026-10-15)

Tracking: [Issue #76](https://github.com/bei-shan/WodeAgent/issues/76).

## Architecture and safety boundary

Inspired by DeerFlow's `E2BSandboxProvider` and provider-owned remote VM
lifecycle, Ridge Code now supports an **opt-in, single trusted Gateway** remote
Bash backend. `AGENT_SANDBOX_MODE=e2b` exposes only the isolated `Bash` tool
and turns off host Read/Write/Edit/MCP/Skills/AgentTeams tools. The host workspace
is **not mounted or synchronized**. Each Agent receives a fresh, randomized VM
identity that is intentionally never adopted from another process or user.

Unlike local Docker/BoxLite, the remote working directory is `/home/user`
(the E2B code-interpreter default). Only `directory="."` or
`directory="/home/user"` is accepted by the command adapter. Existing host
project files are not present and model output must never claim otherwise.
The remote VM has external internet disabled at creation, with server-side
TTL and no host shell fallback. Guest files are **not** automatically copied
into or out of the project.

## Operator opt-in

Install a compatible optional `e2b-code-interpreter` Python SDK and configure:

```dotenv
AGENT_SANDBOX_MODE=e2b
E2B_API_KEY=...     # secret must be supplied by operator secret manager
AGENT_SANDBOX_E2B_SINGLE_GATEWAY_ACK=true
AGENT_SANDBOX_E2B_TEMPLATE=code-interpreter-v1
AGENT_SANDBOX_E2B_TTL_SECONDS=300
AGENT_SANDBOX_E2B_CAPACITY=2
```

The explicit single-Gateway acknowledgement is required. This mode is not
approved when serving multiple Web API worker **processes**, multiple hosts, or
multiple tenants, even if each process configures the same limit. Do not
enable it on a publicly reachable multi-user endpoint.

The E2B credential is read from the process environment only by the provider,
never loaded into `Config`, model arguments, tool outputs, remote VM metadata
or logs. Application deployment must protect the process environment; this
feature does not independently guarantee secret isolation from trusted local
Python plugins.

## State and capacity

A thread-safe **per-process** hard admission ledger reserves a token *before*
VM creation. Remote creation is bounded by a 45-second caller deadline and
server-side TTL defaults to 300 seconds (allowed 120–3600). The metadata
contains two random, per-Agent opaque owner/session identifiers, not a user
email or API key. On normal Agent shutdown the provider asks E2B to kill its
own VM and releases the token **only on confirmed deletion**.

Commands reuse this Agent's remote VM and are sent sequentially, with a
per-command SDK deadline. A cancelled/failed remote command is never retried,
because any remote side effects may already have occurred. The provider asks
E2B to kill the VM; if the create/kill outcome cannot be confirmed, its
capacity token stays reserved and the provider becomes unusable. Subsequent
calls fail closed instead of creating possibly duplicate VMs.

Blocking SDK calls run in daemon workers with bounded caller waits; the
server-side VM timeout is a secondary cleanup guard. A stuck SDK worker may
outlive the caller until the network recovers. The local ledger is deliberately
*not* represented as an organization-wide capacity limit.

## Intentionally missing (required for production parity)

- No Redis-backed **atomic distributed capacity and fenced ownership** yet.
  These are prerequisites before enabling multi-Gateway operation. Never
  initialize multiple processes and rely on this per-process limiter.
- No remote VM discovery, cross-process adoption, or opportunistic deletion.
  Automatically killing an unowned remote ID is a security violation.
- No local-project bootstrap, remote file upload/download, partial sync or
  guest artifact publication. E2B has no shared host workspace.
- No credential refresh/rotation, remote file symlink-safe transfer audit, or
  retry-safe idempotency journal for side-effecting operations.
- No measured multi-tenant workloads or verified E2B template/network
  acceptance in the standard CI job. **This is an experimental adapter**.

DeerFlow's production-grade model has Redis ownership/capacity backends,
remote-list reconciliation, bounded mount hydration and maintenance workers.
Those are significantly more than implementing `Sandbox.create` and
`commands.run`; see their [E2B implementation](https://github.com/bytedance/deer-flow/tree/main/backend/packages/harness/deerflow/community/e2b_sandbox).

## Verification

```bash
python -m pytest tests/test_sandbox_e2b.py -q
# BILLABLE: only on approved isolated test account with E2B_API_KEY set
AGENT_E2B_INTEGRATION=1 AGENT_E2B_ALLOW_BILLING=1 python -m pytest tests/test_sandbox_e2b_integration.py -q
```

CI uses fake E2B SDK objects, so it never calls the cloud or bills for VMs.
The opt-in live SDK acceptance suite requires E2B credentials and two separate\nenvironment acknowledgements before cloud VM creation. Actual execution\nresults must be recorded by an operator; the suite is skipped in standard CI. Verify template availability, networking,
creation/kill failure, concurrent capacity, TTL and no host file sync before
enabling in a real system.

## Next implementation milestones

1. A Redis atomic reservation and fencing service with remote ID journaling,
   no blind lease expiration that could over-allocate still-live VMs, and
   bounded remote reconciliation before declaring shared capacity ready.
2. An E2B-specific file exchange contract with capped bytes and file counts,
   path checks, guest symlink refusal, authenticated upload provenance and
   explicit user approval for committing outputs to a local project.
3. Real-provider and multi-Gateway fault injection, with redacted operator
   evidence and a strict cloud-spend budget.
