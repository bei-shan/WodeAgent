# v0.1 Web shutdown: quiesce → cancel → drain → close

Before this change, the FastAPI shutdown hook iterated
`SessionController.delete_session`. A v0.1 safety fix correctly made
deletion reject running sessions. As a result, backend shutdown could leave
live workers running without an explicit cancellation request. A naive
reversion to force-delete would cause use-after-close of MCP clients,
tool execution state or TraceLogger resources.

## Lifecycle

`SessionController.shutdown(grace_seconds=2.0)` is now the single cleanup
entrypoint for the Web app:

1. Hold the controller lock, mark the controller as shutting down, snapshot
   its sessions. New session creation and attachment are rejected.
2. Mark each session closed under its turn-admission lock. Previously
   acquired session references cannot start another turn.
3. Request cooperative interruption of any busy sessions. This also denies
   and wakes pending permission/AskUser prompts.
4. Await their worker threads up to **one shared grace deadline**, rather
   than a timeout per worker that scales with session count.
5. Close and deregister idle sessions. If a worker remains active,
   **do not close its resources or erase its session data**. Keep a controller
   reference until the process terminates or another drain is requested.

The FastAPI shutdown callback runs this synchronous drain in
`asyncio.to_thread`, avoiding a blocking wait directly on the event loop.

All calls are idempotent after completion. An uncooperative LLM request or
third-party hook is not forcibly terminated. The grace interval bounds
worker `join()` waits, but cannot bound arbitrary misbehaving code inside
a custom `interrupt()` implementation.

## Security and data consistency

Stopping a turn does not guarantee rollback of remote writes, MCP tool
side effects or already-executed subprocess operations. Persisted session
records are not deleted by shutdown, because they must remain resumable
after the next launch.

The interactive cancel path now also wakes/denies pending permissions
when the Agent's own `interrupt()` Hook raises an exception.

## Tests

`tests/test_v01_graceful_shutdown.py` exercises:
- a cooperative Agent worker completing and closing with terminal event,
- an uncooperative worker staying alive past deadline without resource close,
- permission waiters completing immediately when shutting down,
- rejecting new sessions or turn admissions once closing starts,
- actual FastAPI TestClient shutdown invoking the drain,
- a throwing interruption Hook still releasing pending approval waiters.

Release acceptance still requires a real provider/server soak test, and
runtime isolation for truly uncooperative external processes.
