# Team WorkerDispatchService extraction

WorkerSupervisor still owns lifecycle, spawning, stopping and runtime state.
TeamStore owns durable inboxes and work items. ApprovalService owns
the authorization gate. WorkerDispatchService owns the handoff flow:

- Consume inbox messages and maintain cursor / processed message sets
- Deduplicate message-to-work conversion by source message id
- Auto-claim unblocked board tasks into queued work
- Apply plan approval responses and persist approval projections

TeamManager retains its existing private methods as compatibility adapters;
no caller, wire event, retry policy or worker polling interval was changed.
ExecutionService still owns LLM/tool execution and result recording.

Regression: standalone dispatcher contracts, real inbox persistence,
automatic task claiming, plan approval and parallel worker tests in
the full Python suite. Worker polling can be extracted later only after
reviewing interleavings around cancellation, plan approvals and stale work.
