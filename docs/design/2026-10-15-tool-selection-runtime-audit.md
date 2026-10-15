# Tool selection and execution audit — 2026-10-15

Scope: Ridge Code's Python local-trust Agent runtime (`tools/registry.py`,
`core/runtime/tool_runtime.py`, `agents/codeAgent.py`, Feature/MCP adapters).
This is an incremental audit, **not a production multi-tenant certification**.

## Current execution flow

1. ToolBootstrap / MCP / plugin adapters register into a session's `ToolRegistry`.
2. `get_openai_tools()` builds model-visible schemas. The Agent currently
   filters them for plan/delegate mode; MCPs and built-ins are otherwise
   generally advertised eagerly.
3. The model selects the next tool calls (`tool_choice="auto"`).
4. `ToolRuntime.prepare()` invokes ordered Feature/permission pre-hooks on
   the Agent owner thread. Rejected calls get a protocol error; no side
   effecting adapter dispatch occurs.
5. `ToolRuntime.dispatch()` enters the registry and performs sandbox, circuit,
   parameter and adapter checks. Only adjacent tools explicitly marked
   thread-safe may run concurrently; mutating tools remain serial barriers.
6. `publish()` and Agent event/history publication run on the owner thread.

**Tool catalog visibility is not authorization.** The model can emit stale or
unadvertised tool names; execution must fail closed independently of filtering.

## Fixed in this change

| Invariant | Previous condition | New guard / test |
|---|---|---|
| Plan-only mode | Hide unsafe tools from the schema only | `PlanModeFeature.pre_tool_use` blocks before dispatch |
| Public tool-name ownership | Object and function tables could contain the same name, leading to ambiguous schemas | Cross-kind collision rejected at registration |
| Legacy function schema | Declared one string `input`, but forwarded the entire dict and did not validate | Exact one-string contract validated and unwrapped; raw string legacy call remains supported |
| Broken parameter discovery | `get_parameters()` failure silently became empty schema | Broken tool omitted from model-visible catalog instead |
| Recovered MCP tool discovery | ReAct retrieved function schema before runtime MCP reconnection | Build each step's schema after runtime Feature registration |
| Circuit breaker | Bad model inputs must not become tool faults | Invalid function inputs return INVALID_PARAM before dispatch, without recording adapter failure |

Only **cross-kind** registration conflicts are rejected here. Replacing an
existing object tool with another object tool (or function with function)
retains the existing overwrite behavior for compatibility; explicit
registration ownership and revocation must be introduced in a later change.

## Reference patterns and next milestones

- **OpenCode:** canonical tool registration and a settlement/output-bounding
  boundary; location-scoped registrations and permission checks owned by leaf
  executors. See `packages/core/src/tool/AGENTS.md` in `anomalyco/opencode`.
- **Claude Code Best:** tool catalogue and feature-gated built-ins, shared
  operation-specific permission checks; study `src/Tool.ts` and `src/tools.ts`
  before copying orchestration ideas.
- **DeerFlow 2:** configurable tools, MCP + Python providers, deferred
  tool search, and authorization at both catalog-filter and execution
  boundaries. Its hosted authorization options do not automatically certify
  Ridge Code's hosted service.

Recommended delivery order (separate reviews, with test gates):

1. **P0 identity + policy:** derive immutable per-turn tool capability scope
   from authenticated server-side user/project/session identity. Apply it to
   *every* tool entrypoint, MCP, subagent, background worker and plugin, not
   only the schema. Existing local plan/delegate checks are session features,
   not hosted RBAC.
2. **P1 canonical registration:** introduce a single canonical named registry
   with owner/source, immutable schema, policy class, sync/async execute
   adapter, version and lifecycle handle. Duplicate names require an
   explicit priority or namespace; registration replacements and MCP
   reconnects must not revive revoked tools.
3. **P1 selective discovery:** keep a small core always-visible toolset;
   categorize Search/Read/Edit/Execute/Plan/Team/MCP/Skill; provide a
   *post-authorization*, bounded deferred catalog and `tool_search` for
   hundreds of tools. Never allow catalog promotion to bypass the same
   execution policy. Evaluate task-to-tool recall and missed-tool rates
   before turning deferred discovery on.
4. **P1 tool input/output:** converge built-ins, plugins and MCPs on one
   versioned JSON Schema validator, stable error codes, bounded output,
   and managed artifacts. No secret-bearing tracebacks or arguments in
   external telemetry. Preserve existing MCP schema validation.
5. **P1 cancellation/timeout/retries:** per-tool deadlines and cancellation
   propagation; retry *only* marked idempotent calls with bounded budgets.
   Side effects are not automatically rolled back after a cancel/timeout.
6. **P2 schedule and observability:** explicit read/write resource sets to
   prevent parallel reads racing with a write to the same path; fairness and
   backpressure; one correlated call ID across decision, execution and
   publication with sanitized audit events.

## Acceptance requirements

- Model emits a tool hidden by plan/delegate/tenant policy -> side effect
  count remains **zero** and the model receives a stable deny error.
- Registry never produces duplicate public tool names; the advertised input
  schema matches the exact runtime adapter contract.
- Invalid, missing, extra, wrong-type, nested and hook-modified inputs never
  reach side-effecting tools; denied/invalid calls do not trip a circuit.
- Parallel read batching never crosses a write, interactive approval or
  unknown-tool barrier; cancellation closes every tool-call ID.
- Execute realistic tests against live MCP stdio/HTTP, sandbox environments,
  restarted workers, and multiple isolated tenants before any hosted GO.

## Tests for this patch

`python -m pytest tests/test_tool_discovery_execution_guards.py tests/test_tool_input_contracts.py tests/test_tool_runtime_pipeline.py tests/test_tool_three_stage_dispatch.py tests/test_tool_scheduler.py -q`

The pull-request `web-mvp.yml` full Python lane runs `pytest tests/ -q`.
This patch was prepared through the GitHub connector, without a locally
executed Python checkout. CI status is the evidence for automated testing,
not a claim of real-model or production acceptance.
