# AgentTurnLifecycle as turn transaction owner

The Python engine now distinguishes:
- AgentTurnLifecycle: input preprocessing, turn-start/final traces, status,
  automatic snapshot on cancellation or exception, terminal events.
- AgentLoop: ReAct step control, step limit, final output.
- ContextRuntime: prompt and history compression orchestration.
- ModelGateway: LLM provider invocation and feature interception.
- ToolRuntime: tools and pre/post interceptors.
- SessionSnapshotBuilder: canonical persistence schema.

CodeAgent stays the feature composition root and compatibility host.
CodeAgent.run still wraps execution in session-scoped Config context,
and _run_scoped delegates to the lifecycle service. The event names,
status fields and sequence have not been modified.

Tests cover success, cancellation, max-step, exceptions, exactly one
auto-save and terminal event, forwarding and module import boundaries.
The next evolution is explicit immutable RunRequest and state ports rather
than protocol declarations over private host methods.
