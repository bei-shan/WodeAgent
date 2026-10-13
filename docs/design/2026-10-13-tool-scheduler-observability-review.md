# Tool scheduling and observability — source-backed review (2026-10-13)

## Evidence reviewed

- WodeAgent `agents/codeAgent.py::_execute_step_tools` previously executed every model-provided tool call in a serial `for` loop.
- `core/runtime/tool_runtime.py` owns one dispatch with ordered feature interceptors.
- `tools/registry.py` provides circuit-breaker checks, sandbox allowlisting and Read metadata for optimistic locking.
- `core/context_engine/trace_logger.py` already records sanitized JSONL + HTML traces, and `TraceSpan` supports parent/child events.
- [claude-code-best orchestration](https://github.com/claude-code-best/claude-code/blob/main/src/services/tools/toolOrchestration.ts) partitions tool calls using per-tool `isConcurrencySafe(input)`, concurrently processes safe groups, and serially handles unsafe calls.
- [claude-code-best tool contract](https://github.com/claude-code-best/claude-code/blob/main/src/Tool.ts) defaults concurrency safety to false; the MCP discovery path considers read-only annotations.
- [OpenCode tool runtime](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/runtime.ts) validates tool inputs/outputs across an untrusted plugin boundary. This is a useful next-phase consistency target, not code copied into this patch.

## Shipped in this branch: conservative scheduling

1. A bounded, ordered `ToolBatch` planner groups **adjacent** concurrency-safe tools; unsafe tools act as ordering barriers.
2. Tools must explicitly opt in through `Tool.is_concurrency_safe(input)`. Built-in **LS**, **Glob** and **Grep** are opted in after inspecting their read-only implementations. **Read** remains serial because of shared mtime/optimistic-lock caches. **Bash**, **Write**, **Edit**, **MCP**, subagents, user prompts and unknown/custom tools remain serial.
3. Parallel execution is disabled when HookManager has active hooks, when delegate mode or enforced budgets are active, or when an unknown/plugin feature is attached. Malformed input is serial and returns an error for its original call ID. Tool safety classifier errors fail closed to serial.
4. Python `ContextVars` are propagated independently into each worker. Shared CircuitBreaker access is guarded by `RLock`.
5. Tool starts, completions, history and trace writes are emitted on the owning Agent thread. Concurrency only affects dispatch. Model-order tool results are preserved even when completion order differs.
6. Interrupted batches do not execute subsequent calls and still write an `INTERRUPTED` tool result for each model-announced ID. Already running thread operations cannot be forcibly preempted; this remains an explicit limitation.
7. JSONL adds `tool_batch_started` / `tool_batch_completed` entries with mode, tool list, count, worker bound, duration, error count and interruption state.

Config:
- `TOOL_PARALLEL_READS_ENABLED=true` (default for explicitly safe built-ins only)
- `TOOL_MAX_CONCURRENCY=4` (clamped 1–8)

This is intentionally **not** arbitrary parallelism. Cross-call filesystem dependencies, permission requests and custom hook side effects may otherwise produce incorrect state.

## Langfuse or LangSmith?

**Recommendation: preserve local TraceLogger as the default source of audit data; add optional Langfuse export behind an adapter, not vendor APIs inside CodeAgent.**

Rationale:
- Ridge Code has a custom Python Agent engine and is not dependent on LangChain; LangSmith's LangChain-centered workflow is not a compelling reason to adopt that framework just to trace calls.
- Langfuse supports SDK/OpenTelemetry tracing, self-hosting, prompt and evaluation tooling, and provider-agnostic runs. For its Python SDK, use the **v4** API (`get_client()`, `start_as_current_observation`), **not** deprecated `trace()/span()/generation()` constructors.
- Both systems can work with custom instrumentation; do not hardwire one vendor into runtime, UI or domain ports.
- The current JSONL sanitization is not a comprehensive PII scrubber. Cloud export should be opt-in, exclude raw prompts/files/tool payloads by default, and require explicit sampling/retention/redaction controls.

Suggested next interface:
```python
class TelemetrySink(Protocol):
    def start_run(self, run_id: str, session_id: str, attrs: dict): ...
    def record_model(self, run_id: str, step: int, model: str, usage: dict, latency_ms: int): ...
    def record_tool_batch(self, run_id: str, step: int, mode: str, count: int, duration_ms: int): ...
    def record_error(self, run_id: str, stage: str, kind: str): ...
```

Start with a JSONL-backed implementation. Add an *optional* `LangfuseSink` using the current SDK; keep exceptions from telemetry outside the execution failure path. Do not send credentials or raw tool input to third-party telemetry.

## What to measure

- `tool_batch_count`, `tool_parallel_batch_count`, `tool_call_count`, `tool_failure_rate`
- `tool_latency_ms` by name and `tool_batch_duration_ms`; count only actually dispatched calls
- Model latency, first-token latency, provider/model, prompt and completion token counts, total cost **when prices are configured**
- Retry counts, interrupt/cancel counts, permission denials, sandbox denials, circuit-open counts
- Context window size, compression count, truncated output sizes, cache hit rate
- Parent-child relationship: session/run → step → LLM generation → tool batch → tool call → subagent

Never infer monetary cost from token counts without a versioned price table.

## Further reference-driven work (not in this branch)

1. Move all tool input/output schema validation into the registry/runtime boundary; include malformed-model-output recovery tests, following OpenCode's explicit decode/encode contract.
2. Add typed, structured `ToolResult` and immutable call contexts; remove legacy stringly-typed handoff where possible.
3. Add a distinct permission resolution phase before scheduling, with one serial approval gate for potentially interactive actions. Keep sandbox policy enforcement in the dispatch layer.
4. Add cancellation tokens/timeout budgets propagated into subprocesses, Docker and MCP. A future timeout on a Python thread alone does not stop its side effects.
5. Add deterministic tests for same-path read/write conflicts, simultaneous identical read calls, interrupted subagents, non-idempotent retries and tool call ID correlation.
6. Compare DeerFlow's sandbox/workspace ownership and OpenCode's event/runtime APIs with the existing local sandbox; do not copy complex orchestration that duplicates SessionController or ToolRuntime.

## Test plan

- `python -m pytest tests/test_tool_scheduler.py tests/test_step_tool_batches.py tests/test_tool_runtime_pipeline.py -q`
- Regression: `python -m pytest tests/test_permission_gate.py tests/test_sandbox_boundary.py tests/test_event_journal.py tests/test_turn_lifecycle.py tests/test_circuit_breaker.py -q` (when present)
- CI should exercise both Web and TUI to ensure the new parallel UI events preserve ordering.
- External LLM/network integration, Docker hard-cancellation and Langfuse ingest tests are **not** claimed by this patch.
