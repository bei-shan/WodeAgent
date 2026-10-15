# Hosted Worker fencing + Feature architecture review (2026-10-15)

## Changes shipped in this phase

- `core/runtime/hosted_worker.py` is a **standalone, non-HTTP worker
  coordinator**. A trusted server caller supplies a verified HostedIdentity,
  an authoritative membership store, PostgresPrivateTurnQueue,
  RedisTurnAdmission, and a cancellation-aware isolated runner.
- Verify live project membership, acquire Redis global/tenant/user/project
  capacity **before** consuming a durable PostgreSQL claim, and periodically
  renew both leases while the runner works. Each runner receives a bounded
  `LeasedTurn`, the same hosted-only Bash authority, and a cancellation Event.
- If membership or Redis/Postgres renewal fails, signal guest cancellation.
  An expired or revoked fence cannot publish a result. Unexpected runner
  failures are recorded as redacted, terminal `worker_execution_failed`
  rows under the same PostgreSQL fence, preventing automatic replay of
  potentially non-idempotent effects.
- `core/features/lifecycle.py` tracks feature entry before `init`, runs
  `post_init` in the same order, and cleans up in reverse exactly once.
  Constructor errors roll back entered features and owned sandbox resources.

## Why AgentFeature is not itself the wrong idea

Ridge currently puts eleven built-in features and trusted Python plugin
features into one sorted `AgentFeature` chain. The interface combines
`init`/`post_init`/`cleanup` with prompt blocks, pre/post-tool,
LLM intercept and model-change callbacks. Modules mutate private Agent
attributes, while numeric `order` hides dependencies. That works for
single-user extensibility but mixes fundamentally different trust and
failure lifecycles:

| Current concept | Intended long-term home | Guarantee |
|---|---|---|
| MCP, sandbox, session, background runner, TeamManager | `RuntimeService` (typed DI owner) | Explicit dependency graph, start/stop, scoped identity |
| Plan mode, budget, dynamic context, summarization | `AgentMiddleware` | Pure run/step hooks; immutable `TurnContext` |
| Tool and LLM permission enforcement | **Mandatory security kernel** | Cannot be unloaded or bypassed by plugins |
| Hooks, Skills, extra MCP tools, custom adapters | `Plugin` contributions | Manifests, explicit grants, tool provenance, no raw registry |
| Web/UI state and events | Event stream | Event publication after fenced dispatch |

Reference ideas:
- OpenCode: differentiated core tool registry / plugin API, session/tool
  hook events. Plugins should contribute capabilities, not own the whole
  agent lifecycle.
- DeerFlow 2.0: explicit harness/application split and LangGraph middleware
  chain. Note their RFC uses a `RuntimeFeatures` configuration selector;
  the term **Feature** is not inherently problematic. It selects concrete
  middleware implementations rather than giving arbitrary plugins the full
  agent object.
- claude-code-best/claude-code: tools gathered from a central registry,
  conditional flags and discrete tool packages, not one monolithic
  subclass for each capability.

Migration sequence:
1. **Done here:** fail-safe feature lifetime; feature ordering unchanged
   for compatibility. Preserve ToolRegistry security enforcement.
2. Next: typed service container and dependency graph; migrate MCP,
   sandbox, persistence and teams first, with isolated resource ownership.
3. Next: separate ordered middleware chain from plugin-only hook observers;
   distinguish blocking security interceptors from best-effort telemetry.
4. Next: restrict Python plugins to trusted local mode; hosted workers use
   a narrow, explicitly scoped PluginCapability broker, not arbitrary
   `AgentFeature.init(agent)` or host command hooks.
5. Last: freeze/remove the legacy `AgentFeature` API only after parity tests
   and a versioned migration path.

## Important boundaries (NOT production-certified)

This does NOT connect hosted HTTP routes to Agent execution. It does NOT
construct tenant-exclusive workspace mounts or secrets, nor does it
instantiate real CodeAgent runners for the hosted worker. The
`isolated_sandbox` attribute is an assertion by **trusted application
wiring**, not attestation and never user-controlled. A genuine adapter must
verify sandbox provisioning, isolated mount and credentials before enabling
hosted turns. Existing trusted CodeAgent features can still initialize
host-side plugin/MCP/hooks/session resources and are not suitable as a
direct unreviewed hosted runner.

Cancellation is cooperative: a runner must promptly terminate guest
processes, and already completed external side effects cannot be reversed.
Redis and PostgreSQL leases are not one distributed atomic transaction,
so successful publication also requires a valid DB fencing token and
application-side checks for revoked compute leases. A database job's
idempotency key de-duplicates enqueue only; never assume tool side
effects are idempotent. Multi-tenant staging, failover, load, secrets and
tenant sandbox isolation acceptance remain release blockers.
