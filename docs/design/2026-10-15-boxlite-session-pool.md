# Experimental BoxLite session leases and warm micro-VM reuse (2026-10-15)

Issue: [#75](https://github.com/bei-shan/WodeAgent/issues/75)

## Scope

This phase adds an **explicitly opt-in** warm BoxLite VM pool for one Agent.
It follows DeerFlow's dedicated asyncio loop and warm-instance pattern, but
does not yet implement DeerFlow's complete Sandbox file API, cross-agent
pooling, persistent distributed ownership or cloud VM reconciliation.

The normal `boxlite` mode still starts/stops a VM for each Bash call.
The optional warm mode keeps the **same** VM alive between commands of
one Agent, retaining its ephemeral VM filesystem until idle eviction or
Agent shutdown. The only host mount is still the dedicated workspace
at `/workspace`; the guest network remains disabled.

```dotenv
AGENT_SANDBOX_MODE=boxlite
AGENT_SANDBOX_BOXLITE_WARM_ENABLED=true
AGENT_SANDBOX_BOXLITE_WARM_IDLE_SECONDS=60
```

`CodeAgent` creates a random 192-bit scope for each Agent object and gives
the private pool at most one VM. Another Agent receives a different scope and
never gets that instance. Shared pool objects support a bounded capacity up
to 8, but scope identity is a trusted application-layer capability, **not**
an authenticated multi-tenant session ID. Never accept arbitrary scope values
from a model, plugin, browser or untrusted API.

## Lifecycle

- `acquire(scope)` reserves a capacity slot before VM startup. It refuses
  duplicate acquisition and fails closed when all slots are active; it may
  evict only an idle warm VM to make room, never another active session.
- New guests enter `SimpleBox` on a single private event-loop thread.
  Commands run on that same loop; SDK async handles never migrate loops.
- `release(scope)` parks an idle guest. Only exact-scope reacquisition can
  reclaim it, so a different scope cold-boots its own VM.
- The dedicated reaper checks idle guests every 5 seconds. An inactive guest
  past its configured TTL is shut down. TTL 0 destroys the guest on release.
- Timeout, interrupt or SDK failure invalidates the guest and requests
  shutdown, rather than returning it to the pool.
- `CodeAgent.close()` closes the owned pool even if one feature cleanup fails,
  and stops its loop and reaper threads.

Both slot admission and cleanup are bounded. A stuck SDK shutdown may still
leave guest resources behind; the event-loop bridge has timeouts, not a
reliable hypervisor-level kill guarantee. This is not a multi-host HA
solution. Live KVM/HVF tests and stress reporting remain necessary.

## Verification

```bash
python -m pytest tests/test_sandbox_boxlite_pool.py tests/test_sandbox_boxlite.py -q
AGENT_BOXLITE_INTEGRATION=1 python -m pytest tests/test_sandbox_boxlite_integration.py -q
```

The CI tests use a fake SDK and verify warm reuse, session isolation,
capacity exhaustion, TTL expiration, mid-command cancellation,
failed startup, terminal failure cleanup and Agent shutdown. Normal CI
does not prove hardware VM lifecycle correctness.

## Further Issue #75 work

The next milestone is an explicit session-scoped file API executed
**inside the guest**, with bounded binary transfer and symlink-resistant
guest path resolution, followed by actual Linux KVM/macOS HVF soak, scoped
artifact publication and per-session capability enforcement. Host Read/Write
tools and untrusted MCP plugins remain disabled in VM mode.
