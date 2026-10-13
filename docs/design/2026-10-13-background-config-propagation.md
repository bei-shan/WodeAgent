# Background and team session configuration propagation

Config is now passed explicitly to TeamManager -> ExecutionService (including
its per-worker filtered ToolRegistry and team_worker_max_steps), rather than
relying on whichever Agent ran last in the parent process.

ExecutionService scopes the entire work-item use case to its own config
for independently-created teammate threads. BackgroundTaskRunner captures
copy_context() for each launched background task before it spawns a new
thread. An independent copy is made per task, so concurrent launches never
attempt to re-enter the same Context.

ContextVar is not inherited automatically by arbitrary threads; new async
execution adapters must either take Config explicitly or use
bind_current_context() at submission time. A separate process must receive
serialized explicit config rather than a Python context.
