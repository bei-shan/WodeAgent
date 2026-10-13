# Tool contract hardening — 2026-10-13

## Why

The LLM's published function schema is advisory. In the previous path,
`ToolRegistry.execute_tool` normalized responses, but the registry did not
consistently validate parameters before `Tool.run`. Some built-ins perform
their own validation, while plugins/MCP adapters differ. After hook
interceptors rewrite parameters, schema checks must still run at the final
execution boundary.

## Implementation

- Added `core/runtime/tool_contracts.py`, with deterministic validation of
  declared `ToolParameter` names, required fields and primitive shapes
  (including rejecting booleans in integer fields). Values are **never coerced**
  and error diagnostics do **not** echo rejected values.
- `Tool.strict_input_contract` defaults to false, so unknown plugins, MCP
  adapters and custom Python tools retain existing compatibility.
- First opt-in cohort: Read, LS, Glob, Grep, Bash, Write, Edit, MultiEdit.
  These tool schemas have been inspected and their validation exercises will
  be included in the full Python CI suite. Optional fields may be null.
- In `ToolRegistry.execute_tool`, checks run **after** the Docker allowlist,
  optimistic-lock metadata injection and circuit-open gate, and **before**
  executing a side-effecting adapter. Hook changes are captured because
  `ToolRuntime` sends its final parameters into the registry.
- Unknown field rejection follows existing `additionalProperties=false`
  published schema for opted-in tools; no default values are materialized,
  which avoids mutating existing tool output context.
- Central `error_envelope` now supplies standard
  status/data/text/error/stats/context for blocked hook calls, parse failures,
  dispatch exceptions and pending calls cancelled by a user interruption.
- Every input contract error returns INVALID_PARAM and does **not** advance the
  tool's circuit breaker.

## Security boundaries that remain

Schema validity does **not** imply tool permission. PermissionGate, Docker
sandbox allowlists, worktree boundaries, per-tool capabilities and explicit
hooks remain independent execution controls. The registry checks final inputs,
not the initial model suggestion. Interactive approval must remain serial.
Sandboxing is still opt-in; a successful schema check must not authorize host
command execution.

## Limitations / next steps

1. The cohort intentionally excludes multi-purpose, mode-dependent tools
   (e.g. Skill list/create, Task persistent/parallel) until their published
   required-field contracts are corrected.
2. Validate richer nested JSON schemas for MCP and plugin tools once their
   source schema can be trusted and normalized. Keep bounded error messages.
3. Define immutable `ToolInvocation` and structured `ToolResult` objects and
   separate permission resolution, execution and result publication.
4. Later add optional Langfuse (OpenTelemetry) export behind a non-blocking
   telemetry adapter, with redaction and opt-in control. Current local JSONL
   remains the default source of truth.
5. Executing parallel reads still requires audited thread safety; schema
   validation alone never opts a tool into concurrent execution.

## Source references

- Claude Code Best: `src/services/tools/toolOrchestration.ts`,
  `src/Tool.ts` (per-tool concurrency safety, serial barriers)
- OpenCode: `packages/core/src/tool/runtime.ts`
  (validate/encode at the untrusted tool boundary)
- Existing Ridge Code: `tools/registry.py`,
  `core/runtime/tool_runtime.py`, `core/runtime/tool_scheduler.py`

## Tests

`python -m pytest tests/test_tool_input_contracts.py tests/test_tool_runtime_pipeline.py tests/test_tool_scheduler.py tests/test_step_tool_batches.py -q`

The refreshed `web-mvp.yml` full Python job also covers all Python tests.
External LLM integrations, MCP remote hosts and stress workloads are outside
this change.
