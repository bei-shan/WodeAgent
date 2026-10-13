# MCP schema and permission boundary hardening (2026-10-13)

## Problem

The MCP adapter previously only checked required/unknown top-level fields
and flattened remote schemas to `ToolParameter` entries for model tool
advertisement. That lost nested `properties`, union and array item
constraints, `enum`, and other JSON Schema semantics.

Path grants in PermissionGate also used raw string cache keys. Symlink
aliases and `..`-normalized paths could access the same file under different
authorization records. Worse, a direct call to `ask()` could prompt for a
permanently denied path without checking `check()` first.

## Implemented

- `tools/mcp/schema_validation.py`: compile and validate Draft 2020-12 JSON
  Schema using explicit `jsonschema>=4.20,<5` dependency. Supports nested
  objects, arrays, enums, alternatives and local `#/$defs` references.
- Untrusted schemas are bounded to 128 KiB, 4096 nodes, depth 32. External
  `$ref`/`$dynamicRef`/`$recursiveRef` are rejected to avoid remote
  reference resolution. Bad schemas fail closed for both advertisement and
  execution; connection discovery itself remains backward-compatible.
- `MCPToolAdapter.get_input_schema` supplies the authoritative (defensively
  copied) schema to `ToolRegistry.get_openai_tools`. Existing built-in
  `ToolParameter` paths are unchanged.
- MCP arguments are validated immediately before a remote call without
  value coercion. Error diagnostics expose only validation *locations and
  keywords*, not values echoed by jsonschema.
- `MCP_PARAM_ERROR` is recognized as client input error by the circuit
  breaker: repeated model mistakes do not trip a remote tool's circuit.
- PermissionGate uses resolved, canonical cache keys with a shared RLock for
  main/subagent views; permanent-deny checks use both source and canonical
  target. `ask()` checks the policy before invoking an interactive broker.

## Boundaries and limitations

JSON Schema validation is **not authorization**. MCP tools still execute with
the remote server's granted capabilities. A user consent/permission policy for
individual remote MCP actions requires a separate explicit policy surface.

The schema guard disallows external JSON references, but it is not a general
isolation environment for untrusted MCP servers. Some JSON Schema regular
expressions may be computationally expensive; larger untrusted-schema
resource limits, process isolation and per-tool capability grants are follow-up
work.

`PermissionGate` canonical resolution reduces aliasing mistakes but cannot
eliminate time-of-check/time-of-use races against adversarial filesystem
mutation. Hardened sandbox boundaries should use OS isolation.

Existing MCP client timeouts apply per request (30 seconds default), but this
change does **not** promise that cancelling an Agent turn forcibly kills
already-running Bash or MCP operations. Adding explicit cooperative
cancellation with process-group teardown is the next runtime track.

This change adds no Langfuse SDK or external telemetry sink: local trace
remains the only default and remote export must be opt-in and redacted.

## Regression tests

- `tests/test_mcp_schema_boundary.py` — nested validation, ref handling,
  secret-safe diagnostic messages, unadvertised bad schemas, no remote
  execution for invalid parameters and circuit immunity.
- `tests/test_permission_canonical_boundaries.py` — path aliases, symlink
  sensitive target denial, ask-before-broker and shared cache locking.
- Full Python, API Smoke, Playwright, Web build and Docker sandbox CI.
