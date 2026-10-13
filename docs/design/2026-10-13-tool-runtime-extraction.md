# Engine ToolRuntime extraction — 2026-10-13

## Scope and contract

One tool execution has been removed from CodeAgent and is owned by
core/runtime/tool_runtime.py.

The new ToolRuntime accepts:
- ToolDispatcher port with execute_tool(name, parameters)
- Ordered ToolFeature interceptors (pre_tool_use/post_tool_use)
- Explicit agent host (opaque; no CodeAgent import)
- Message publishing callback
- Timing callback
- Logger

CodeAgent remains the run/step orchestrator and event source. Its
_execute_tool adapter only wires dependencies; no ReAct loop, event protocol,
permission or sandbox behavior is reimplemented.

## Preserved behavior

- Non-dict tool parameters become an input argument.
- PreToolUse hooks execute in order; input updates are cumulative.
- An early block returns the same error JSON and bypasses tool dispatch and
  timing registration.
- PostToolUse hooks run in the same order and may emit system messages.
- Execution timing is recorded before PostToolUse as in the old implementation.
- ToolRegistry is still the enforcing dispatcher for sandbox allowlist,
  optimistic lock injection and circuit-breaker policy.
- Existing event publisher still owns tool.started/tool.completed.

## Quality checks and next steps

New unit tests verify ordered pre/post hooks, updated args, early blocks,
dispatcher exceptions, CodeAgent compatibility and engine import boundaries.
Web and Sandbox integration suites continue to run unchanged.

Next, move LLM/model calls and context compaction behind typed ports, then
extract AgentLoop from CodeAgent in smaller commits with deterministic
fake-model turn fixtures. Keep tools, approval, sandbox and event interfaces
stable across the migration.
