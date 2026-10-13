# Cooperative Bash cancellation and process-group teardown — 2026-10-13

## Before

Agent `interrupt()` only set a thread event for the step loop.
`BashTool.run` called blocking `subprocess.run(..., timeout=...)`,
meaning an interrupted command could keep working (and modifying files)
until a timeout of up to 10 minutes, even if the UI said "Stopped".

## Change

- `core/runtime/cancellation.py`: per-invocation ContextVar with scoped
  set/reset. The active Agent event is never a model-supplied tool parameter.
  Concurrent agents do not share one process registry or cancellation flag.
- `CodeAgent._execute_tool` scopes its cancellation event around the full
  ToolRuntime dispatch. The scheduler can propagate ContextVars to safe
  worker threads without leaking one turn's event to another.
- `core/runtime/process_execution.py`: `Popen` + bounded polling of
  `communicate(timeout=...)`. On user interruption, kill the process
  and return an interrupted outcome; on timeout, terminate the process
  and preserve partial stdout/stderr semantics.
- POSIX: create a new session for each command, then `killpg(SIGKILL)`
  to stop shell children and grandchildren in that group. Windows: use a
  new process group and terminate the direct child; full descendant cleanup
  needs a future Windows Job Object backend.
- `BashTool` emits a structured `INTERRUPTED` error on cancellation.
  Expected cancellations do not count as circuit breaker failures.
- Regression suite covers active Agent interruption, no-start if already
  cancelled, preservation of timeout partial output and POSIX grandchildren.

## Honest limitations

- This makes **Bash subprocesses** cooperatively cancellable; it does not
  forcibly preempt remote MCP servers, LLM provider requests, interactive
  dialogs, non-Bash tools or background task workers.
- Child processes that intentionally leave the original POSIX session or
  group may escape `killpg`. A hardened Docker sandbox remains essential
  for malicious workloads.
- Between the initial permission check and process creation there is a
  tiny race; after spawn, the polling loop will observe a requested cancel.
- Cancellation polling uses roughly 100 ms intervals; child termination is
  additionally bounded by pipe-drain cleanup.
- An already-completed tool cannot be retroactively cancelled.
- No process execution capability was added to the LLM or MCP surface;
  existing Bash sandbox and shell parsing policies still apply.

## Follow-ups

1. Use a Windows Job Object to kill an entire command tree.
2. Extend the runtime cancellation port to MCP client `asyncio` tasks,
   stream generators, and the broader background execution lifecycle.
3. Add trace metadata for interruption cause and per-tool elapsed duration,
   with opt-in sanitized OpenTelemetry/Langfuse export later.
