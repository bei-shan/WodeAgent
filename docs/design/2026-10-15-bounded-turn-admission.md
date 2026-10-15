# Bounded local Agent-turn admission (2026-10-15)

> **Shipping scope**: current Web/local single-process runtime only. This is neither
> high-availability scheduling nor authenticated multi-tenancy.

## The problem

Before this change `AgentSession.send_message()` launched one Python thread
per active session, with only a *per-session* busy flag. `TOOL_MAX_CONCURRENCY`
limited tools inside one agent, not the total number of simultaneous turns.
Cold initialization also ran synchronously on the FastAPI event loop.

A small number of conversations can therefore exhaust worker threads, local
CPU/memory, LLM rate limits, or file descriptors, especially when a turn is
blocked waiting for a permission prompt or a provider call.

## Behaviour implemented

- `TurnAdmission` is a synchronous thread-safe **non-blocking** lease counter.
- `SessionController` owns one admission instance, shared by *all* local
  sessions, including re-activated ones.
- `RIDGE_MAX_ACTIVE_TURNS` controls the local process limit (default `8`,
  integer `1..256`). Invalid configuration fails application startup.
- Under saturation, a **different idle session** receives HTTP **429** and
  `Retry-After: 1`; the rejected session remains idle and does not lazily
  initialize an Agent. A busy session still returns 409, unchanged.
- `/api/health` exposes only process-wide numeric
  `turn_admission.active / limit / rejected`; it does **not** expose owner keys,
  credentials or prompts.
- Permit release occurs after terminal events, and also on cold initialization,
  pre-worker interruption, thread creation or worker execution failure. A
  duplicate `release()` never underflows the counter.
- The Web message endpoint runs the potentially blocking lazy initialization
  in an asyncio worker thread, not on the ASGI event loop.
- No in-memory waiting queue is created. Under saturation clients may retry or
  stop. UI retry logic must respect `Retry-After`, not spin aggressively.

The `max_per_owner` primitive supports scoped counts **only if a trusted
server component supplies the owner key**. The Web runtime has only a
single-operator login token; **all current turns use one trusted `local` owner**.
Untrusted HTTP headers, model arguments and user-supplied session JSON **must
never** determine the accounting owner or grant access to resources.

## What remains before multi-tenancy

1. OIDC user principal verified server-side, membership checks, RBAC/ABAC
   rules for every session and WebSocket/SSE + workspace/skill/trace/MCP route.
2. Replace JSON session state with PostgreSQL transactions, unique
   `tenant_id, project_id, session_id` constraints and optional row-level security.
3. Introduce durable task records, a worker-pool scheduler and globally shared
   permits/leases with fencing tokens. **Do not** raise Uvicorn worker count
   to solve throughput: process-local admission counters are independent.
4. A tenant/user/project quota plan for request rate, active turns, token
   budgets, tool CPU/memory, sandbox leases and storage; fair scheduling,
   backpressure, cancellation and retries with idempotency keys.
5. Persist events with replayable checkpoints and an outbox for cross-replica
   SSE; prevent orphaned tasks after worker crash/restart.
6. Conduct real-provider tests and mixed-tenant soak before promising
   throughput. Report P95/P99, active/rejected turns, queue wait (if added),
   RSS, threads, rate-limit errors and isolation/security checks.

## Verification

`tests/test_turn_admission.py` covers thread contention, two synthetic
owner limits, fail-closed config, cold-start failures, thread-start failures,
turn exceptions, HTTP 429/Retry-After and successful reuse. This does not
simulate real OIDC tenants or stress distributed workers.

Recommended developer command:

```bash
python -m pytest tests/test_turn_admission.py tests/test_v01_graceful_shutdown.py tests/test_web_service.py -q
```

No secrets, model calls or Docker daemon are required for these tests.
