# Tool execution: Prepare → Dispatch → Publish

Earlier batching reused `_execute_tool` within ThreadPoolExecutor workers,
which meant Feature `pre_tool_use` / `post_tool_use` callbacks could run on
worker threads for safe reads, potentially mutating Agent state concurrently.

This change makes the ownership boundary explicit:

1. **Prepare / permission decision (Agent owner thread)**:
   parse arguments, run ordered `pre_tool_use` feature callbacks,
   apply `updated_input`, emit requested system messages, and return a
   frozen `PreparedToolCall`. Denials produce a structured error envelope
   without executing the dispatcher. Permission-sensitive file tools remain
   sequential and retain last-mile access checks at their execution boundary.
2. **Dispatch (worker or owner thread)**:
   `ToolRuntime.dispatch` invokes only `ToolRegistry.execute_tool`.
   No feature callbacks, history writes or UI event publication are permitted.
   Registry sandbox permissions and final strict contract validation remain
   the final execution guards. OTel tool observation wraps this I/O phase.
3. **Publish (Agent owner thread)**:
   `ToolRuntime.publish` runs `post_tool_use` feature callbacks and records
   elapsed time, then CodeAgent commits Trace, UI events and HistoryManager
   output in stable model call order, preserving tool_call_id.

For concurrent batches, all preparations finish on the owning thread before
the pool starts. A prepared input that loses the tool's opt-in
`is_concurrency_safe` guarantee forces serial dispatch. A tool whose
interceptor denies execution cannot run. Unknown/plugin tools still default
to serial. Standalone `ToolRuntime.execute` preserves its prior convenience
interface by running all three phases sequentially.

A direct test override of `CodeAgent._execute_tool` retains the legacy
single-method path to support host adapters and tests; normal production
CodeAgent uses the staged path.

This refactor does not claim fully distributed transactions: external MCP
servers and Bash commands can have side effects even if local result
publication fails. It does ensure that owner-thread publication is
not accidentally performed inside read workers.
