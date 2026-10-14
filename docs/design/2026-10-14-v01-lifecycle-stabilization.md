# Ridge Code v0.1 stabilization — interactive cancellation and durable turn lifecycle

## Runtime correctness fixes

### Cancel permission/AskUser waits immediately

Before this change, `AgentSession.interrupt()` only called the Agent's
`interrupt` method. A turn suspended inside a permission broker could
remain blocked for 120 seconds; AskUser could block for 300 seconds.

`AgentSession` now owns a per-turn interaction-cancellation Event, reset
only as a new turn is admitted. An interrupt sets that event, invokes the
Agent's cancellation hook, rejects late UI replies and wakes pending
permission/AskUser waiters with safe defaults (permission denied; AskUser
empty). The brokers check the event while registering to close the
interrupt-before-registration race and check again before returning any
previously granted answer.

This is **cooperative cancellation**: it cannot force a remote provider
to roll back actions already executed. A prompt event already transmitted
to a browser may still be visible briefly; its late resolution is refused.

### Don't delete running sessions

A running Agent may be writing a conversation snapshot, task board, tool
output or trace. Deleting the session at that point used to remove both its
workspace and its backing session data and close its resources.

`SessionController.delete_session` now checks the same busy lock that
guards `send_message` and marks an idle session closed before removal.
Previously acquired session references are prevented from starting turns
after deletion. `SessionService.delete` translates a busy result into HTTP
409 without deleting the catalog record or workspace. Clients should call
`POST /api/sessions/{sid}/interrupt`, wait for `turn.finished`, then
retry deletion.

### Make turn publication and files durable

`AgentSession._run_turn` now publishes terminal journal events before
clearing the busy flag. Consumers polling `busy=false` will not observe
a completed outcome before its terminal journal entries exist.

`persist_turn_outcome` uses independent same-directory secure temporary
files, flush+fsync and `os.replace`. Concurrent writers cannot overwrite
one another's temporary path. Failure to replace leaves the previous
successful snapshot intact; unused temporary files are cleaned up.

## Regression matrix

- `tests/test_v01_session_lifecycle_reliability.py`: live-thread permission
  and AskUser interruption; pending prompt cleanup; no late approvals;
  cancellation before prompt registration; idle/new-turn boundaries;
  HTTP 409 on running session deletion; stale-session reference rejection.
- `tests/test_v01_turn_outcome_durability.py`: concurrent writers, always
  valid on-disk JSON snapshots; failure injection during atomic replacement;
  session identifier validation.
- Original `tests/test_turn_lifecycle.py`, `tests/test_web_interrupt.py`,
  `tests/test_session_tree_api.py` plus full Python/Browser/Docker CI.

## Explicitly not yet covered

- Force-stopping arbitrary LLM providers or remote MCP servers that ignore
  cancellation; the local MCP awaiter is cancellable but remote effects
  may continue.
- An end-to-end soak test with live provider accounts and production
  Langfuse credentials; those must be configured by an operator.
- Cross-process leader election / multi-worker server hosting. The Web API
  still uses a process-local SessionController and is intended for trusted
  single-operator use, not multi-tenant distribution.
- Abrupt power-loss durability for directory metadata; fsyncing a file is
  not equivalent to guaranteeing that a containing directory is durable
  across every storage system.
