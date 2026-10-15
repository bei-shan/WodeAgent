# Runtime Service ownership migration — 2026-10-15

## Implementation

The old `AgentFeature` shape conflated resources, prompt decoration, policy
and third-party Python extension code. This migration introduces
`core/runtime/services.py:RuntimeServices`: a per-Agent, framework-owned,
explicitly registered dependency graph. Services start once; each is recorded
before `start()`, so a partial initialization failure causes LIFO cleanup.
Unknown dependencies and dependency cycles are rejected. After a failure the
graph cannot silently restart in a partially initialized state.

Four stateful owners have moved out of Feature methods:

| Service owner | Resource responsibility | Legacy Feature adapter |
| --- | --- | --- |
| `service_mcp.MCPService` | MCP clients, retries and owner-scoped tool removal | `MCPFeature` |
| `service_teams.TeamService` | TeamManager startup and shutdown; depends on MCP | `AgentTeamsFeature` |
| `service_sessions.SessionService` | Local SessionManager creation/snapshot cleanup | `SessionFeature` |
| `service_sandbox.SandboxService` | Docker/BoxLite/E2B provision, Bash registration, sealing, VM close | Directly owned by CodeAgent tool bootstrap |

Old `AgentFeature` entry points remain as adapters for locally trusted
existing plugins. This is a staged migration, not removal of the API.
`CodeAgent.close()` closes its Feature lifecycle and its service graph,
and retains a fallback VM cleanup for legacy instances.

## Hosted security

A verified hosted identity **does not enable the local Agent runtime**.
At constructor time hosted Agents skip host-resident MCP, hooks, worktrees,
teams, background tasks, VCR, output style resource reads, local session
creation, Python plugins and host tool bootstrap. Only lightweight plan,
delegate and budget policy adapters run; a restricted isolated Bash adapter
is registered. `CodeAgent.run` refuses hosted turns until an audited,
identity-scoped, fenced worker/isolated session adapter is connected. The
hosted HTTP API remains metadata-only.

A plugin is not permitted to bypass an authenticated and fenced runtime by
accessing arbitrary mutable `Agent` private fields. The long-term extension
API must use explicit, typed capabilities rather than raw `AgentFeature`
references on a hosted worker.

## Next migration

- Separate Plan/Budget/Context/LLM tracing into an explicit typed
  `AgentMiddlewarePipeline` with immutable per-step context.
- Move worktree/background/hooks ownership into audited services before
  deleting Feature adapters.
- Build an actual tenant-exclusive sandbox/session adapter for
  `HostedTurnWorker`, then deliver authenticated queue APIs and real
  two-tenant, cancellation/failover and capacity acceptance.
- Split deny-by-default security decisions from best-effort plugin events.
- Add an end-to-end sandbox attestation and scoped credential broker.

CI validates local backward compatibility, dependency order, graph failure
rollback, hosted startup isolation, and the existing Docker/BoxLite/E2B
contracts. It is **not** a production certification of hosted execution.
