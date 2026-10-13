# Phase 5: Session-scoped configuration (2026-10-13)

## Why

CodeAgent used to call three module setters at initialization, mutating
process-level configuration for ObservationTruncator, ToolRegistry and Task.
The last initialized session could change another session's truncation,
circuit breaker and subagent policies during parallel turns.

## New ownership model

- Agent.__init__ deep-copies a caller-provided Config before storing it.
- ToolRegistry receives Config in its constructor or at initial agent setup;
  circuit breakers are per registry instance, not taken from the last Agent.
- TaskTool gets the owning Config via ToolBootstrap dependency injection;
  it applies local subagent limits and light-model settings.
- scoped_config uses ContextVar for synchronous Agent turns, including
  truncation helpers that previously only read a module-wide singleton.
- CodeAgent.run binds the scope in a try/finally context and restores it.
- Legacy setter functions remain for standalone consumers and tests, but
  CodeAgent no longer calls them during construction.

## Compatibility

No HTTP, TUI or WebSocket protocol changes. Core Agent functionality remains
Python. Existing CLI entrypoints can still pass Config, now copied privately.

## Limitations and future work

ContextVar does not automatically propagate to arbitrary spawned threads or
separate processes. Explicit config injection is the preferred long-term
interface for all tools, subagent runners and sandbox providers.

A ToolRegistry must belong to one independently configured Agent. Sharing a
single mutable registry across sessions is unsupported and should be prevented
at the Application Service composition root.

Next: remove all remaining singleton setters in favor of typed ports,
standardize immutable AgentOptions per session, and split AgentLoop and
ToolRuntime from CodeAgent.
