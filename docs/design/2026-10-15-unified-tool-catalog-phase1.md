# Unified Tool Catalog — Phase 1 (2026-10-15)

## Motivation and reference designs

Ridge Code already contains the necessary execution boundaries: registered
objects/functions/MCP adapters; a `ToolRuntime` Prepare → Dispatch → Publish
pipeline; a serial-barrier/parallel-read scheduler; sandbox and permission
checks. Replacing this with a second framework would create two executable
sources of truth.

This opt-in milestone combines:

- **OpenCode** (`packages/core/src/tool/runtime.ts`,
  `packages/core/src/tool/AGENTS.md`): model definitions and executors
  originate from a single owning registration; inputs are validated at runtime.
- **Claude Code Best** (`src/services/tools/toolOrchestration.ts`):
  only explicitly concurrency-safe reads overlap; writes and interactive
  approvals are serial barriers.
- **DeerFlow** (`deferred_tool_filter_middleware.py`): deferred tool
  schemas must be hidden from model binding *and* blocked at tool dispatch
  until discovery promotes them. Filtering is not authorization.

## Current contract

```text
ToolBootstrap / MCP / plugins
       |
       v
ToolRegistry  <-- one authoritative schema + executor source
       |
       +--> model-visible catalog (core + promoted deferred tools)
       |       +--> model tool choice
       |
       +--> ToolRuntime.prepare (owner-thread Features: Plan/Delegate/etc.)
                 |
                 v
            ToolRegistry.execute_tool
                 |-- sandbox boundary
                 |-- dynamic mode guard (opt-in)
                 |-- deferred admission (opt-in)
                 |-- schema / circuit / tool runner
                 v
            ToolRuntime.publish (owner-thread events/history)
```

On `TOOL_DEFERRED_DISCOVERY_ENABLED=true`, `ToolSearch` is exposed to
the model and takes `query: string` and optional `limit: int` (1–6).
Search is bounded to existing, schema-valid, sandbox-visible,
circuit-available, mode-authorized entries. Matching is deterministic,
name-first casefolded lexical matching with description fallback, not a
semantic vector search. Promoted names live in one Agent-owned
`ToolDiscoveryState`; they are not global or shared between sessions.
The default promotion budget is 18 names. Search matches are **pending** until
`CodeAgent._get_openai_tools_for_current_mode()` binds the next model-step
schema. A second guessed tool call in the same response is still denied.
A pending promotion is rechecked against the authoritative current registry
and mode policy before activation.

The always-visible compatibility core is Read, Write, Edit, MultiEdit, LS,
Glob, Grep, Bash, TodoWrite, AskUser, Task, TaskOutput, Skill,
EnterPlanMode, ExitPlanMode and ToolSearch. Tools added later by MCP can
be searched when their valid schemas become available.

**Registration replacement and unregistration revoke promotion.** The
execution boundary checks the same dynamic Plan/Delegate mode closure used
to select schemas, even on direct registry calls. Existing PreToolUse
Feature hooks remain in place and may still deny, modify or ask. The
sandbox allowlist runs before any discovery promotion.

The ContextBuilder projects only visible tools' usage notes in L1 and
shows concise ToolSearch guidance instead of a listing of all deferred MCP
tools. A ToolSearch result is not evidence of execution permission.

## Enable / rollback

```sh
# Local trusted single-user only; OFF by default
TOOL_DEFERRED_DISCOVERY_ENABLED=true
TOOL_DISCOVERY_MAX_PROMOTED=18
```

Turn the flag off and restart the Agent to immediately restore full
legacy tool schemas. In non-local sandbox modes discovery is not
activated, so no new host tool gets permitted via ToolSearch.

## Tests and release posture

Run:

```sh
python -m pytest tests/test_deferred_tool_catalog.py \
  tests/test_tool_discovery_execution_guards.py \
  tests/test_tool_input_contracts.py \
  tests/test_tool_runtime_pipeline.py \
  tests/test_tool_scheduler.py -q
```

CI additionally runs the full Python suite, MCP compatibility, isolated
Docker smoke, Web and TUI checks. Explicit assertions cover:

- unpromoted tool side effect count remains zero;
- promoted schema and runtime agree, and same-response guessed tool calls
  remain denied until the next model-step schema binding;
- role/mode change revokes execution even for a previously promoted name;
- two registries with equal names cannot share promotions;
- replacing/removing an executor invalidates a promotion;
- bounded search requests cannot exhaust catalog exposure;
- hidden tool usage notes are not sent to the model.

This is NOT production approval. The refactor still uses existing mutable
registry maps for adapter ownership, legacy function wrappers and local
Feature hooks. Next stages should unify registration ownership/disposal
handles, snapshot executor identity/version per model call, split true
hosted-tenant authorization from visibility, add provider-specific
capability/effect declarations, resource-aware conflict scheduling, MCP
lifecycle fencing, output schema/retention contracts and objective
tool-retrieval + end-to-end task-success evaluation. Existing local mode
cannot be exposed as a multi-tenant hosted executor.
