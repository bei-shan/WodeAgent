# Hosted turn crash semantics: never automatically replay uncertain effects

**Date:** 2026-10-15

## Failure model

A coding-agent turn can write files, invoke shell commands, call an external
service, or emit irreversible side effects *before* its PostgreSQL lease
expires. Losing a Redis permit or PostgreSQL lease does **not** establish that
those effects did not occur. A fencing token blocks stale **database
publication**, but by itself it cannot roll back guest filesystem writes,
remote API effects or external messages.

Before this change `PostgresPrivateTurnQueue.claim()` reclaimed a
`running` row once `lease_until` elapsed and attempts were below three.
That could execute the same non-idempotent work twice.

## New invariant

**Each queued turn may be claimed only once.** A timed-out `running` row
is marked `failed` with the redacted error kind
`lease_expired_uncertain`, its `fence` increments and its lease
credentials are cleared. It is never automatically returned to a new worker.
The active worker's subsequent `renew`/`complete`/`fail` is rejected.
`reap_expired()` allows bounded, tenant-RLS-scoped cleanup even when there
are no new jobs to claim.

The original payload and specific error/secret are not copied into the
terminal failure record. The idempotency key still deduplicates repeated
`enqueue` requests for an **identical** payload, but never proves tool
side-effect idempotency.

If an operator/user wants to try again after examining the effect, they
must create a **new** job with a new idempotency key; do not silently retry
an uncertain result.

## Follow-up release gates

- An isolated runner must prove it terminates guest processes on lease loss.
- Persist and expose an explicit `uncertain` outcome to Web/API clients.
- A real hosted worker needs a tenant-unique sandbox, scoped credentials,
  authenticated queue admission and cancellation fencing.
- PostgreSQL RLS live integration, cross-tenant security, Redis-fault chaos
  and non-idempotent side-effect tests must pass on staging.
- This conservative behavior may reduce availability after network blips;
  it prioritizes at-most-once **attempt** admission over false promises of
  exactly-once side effects.
