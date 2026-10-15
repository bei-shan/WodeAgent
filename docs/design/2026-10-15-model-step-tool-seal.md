# Model-step tool capability seal — 2026-10-15

## Why

Tool discovery and model-visible JSON schema must refer to the **same adapter**
later used for a tool call. MCP reconnect, tool registration replacement, plugin
reload, or a mutable adapter's schema update can occur while awaiting the model.
A name-only dispatch can then call code that was never shown to the model.

## Boundary

- `ToolRegistry.capture_tool_step()` takes the registration lock, evaluates
  the current sandbox, mode, circuit and discovery filters, and seals each
  advertised schema (strict canonical JSON) together with the *identity* of
  its registered execution adapter.
- `CodeAgent._get_openai_tools_for_current_mode()` captures the step and returns
  independent JSON objects to the LLM. Agent-owned worker dispatch carries that
  exact seal into `ToolRuntime`.
- `ToolRegistry.execute_tool(..., snapshot=seal)` fails closed with
  `STALE_TOOL_SCHEMA` if a name was not advertised, its registered adapter
  changed, or its schema/description drifted since capture.
- The registry locks adapter selection and version validation together; it
  releases the lock **before running the adapter** so safe read concurrency
  is preserved. The selected adapter is stored in a local, not looked up
  again after unlocking. Already-started external I/O cannot be revoked.
- Current sandbox, dynamic Plan/Delegate policy, deferred-discovery visibility
  and circuit checks remain mandatory; a seal is **not** user/tenant
  authorization and grants no new rights.
- Direct legacy calls without a snapshot retain the previous behavior.
  Model-supplied calls through the canonical CodeAgent batch path are sealed.
  Third-party overrides of `_execute_tool` are compatibility paths and
  must enforce their own equivalent security boundary.

## Explicit acceptance scope

Tests cover mutation of returned model schemas, adapter replacement/removal,
function tools, in-place schema change, newly registered names, cross-registry
isolation, discovery activation between model steps, failed mode filters and
permission revocation. CI additionally validates the existing scheduler and
tool runtime regressions.

## Not yet solved

A strict owner/capability policy for MCP, plugins and built-in registrations;
all tenant identity and authorization enforcement across server/worker paths;
cancellation of already-started calls; and evaluation of model tool selection
accuracy remain separate production-readiness gates.
