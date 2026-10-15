# Mandatory tool-policy middleware — 2026-10-15

## Purpose

The legacy `AgentFeature` chain mixed mandatory security decisions and
optional plugin interception. Sorting by a mutable `order` number let a
Python plugin run before the Plan, Delegate or enforced Budget gates,
potentially performing side effects even when the eventual tool was denied.

## Changes

- `core/runtime/policy_middleware.py`: state and policy logic now lives
  in standalone `DelegateModeMiddleware`, `BudgetMiddleware` and
  `PlanModeMiddleware` implementations.
- `core/runtime/middleware.py`: one framework-created
  `PolicyMiddlewarePipeline` initializes all core policies before the
  Plugin/Feature lifecycle. Plugins do not receive a registration API for
  this mandatory policy chain.
- `ToolRuntime.prepare()`: mandatory policy checks run before any
  third-party Feature.pre_tool_use callback; unavailable or throwing
  core policies deny with `PERMISSION_DENIED`. Failed decisions never
  dispatch or run user-contributed hook callbacks.
- `PlanModeFeature`, `DelegateModeFeature`, `BudgetFeature` are
  compatibility facades around policy implementations, preserving existing
  user-facing calls and model-change/runtime prompt behavior.
- Regression tests cover plan read-only deny, delegate deny, budget deny,
  plugin hook order, policy outage and allowed-hook parity.

## Security boundary and next work

This is defense in depth, **not** authorization by itself. The mandatory
identity and tool-snapshot checks in ToolRegistry still apply, and hosted
Agent execution remains gated pending an audited worker and resource-level
MCP/plugin/sandbox contracts. Arbitrary trusted local Python plugin code
can manipulate runtime private state; such code is explicitly not permitted
in hosted execution.

Follow-on work: move ContextBuilder assembly and LLM interceptors into
typed per-step Middleware; add immutable context snapshots and dedicated
telemetry observers. Do not put trusted system authorization policy into
the untrusted plugin ABI.
