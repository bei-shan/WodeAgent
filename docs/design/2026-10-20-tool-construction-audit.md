# Tool construction and execution audit — 2026-10-20

## Scope and references

Audited the current `main` implementation of `Tool`, `ToolBootstrap`,
`ToolRegistry`, `ToolRuntime`, `ToolScheduler`, `Bash`,
`run_process`, `Read`, `Write`, `Edit`, `Grep` and permission gates.

Design comparisons (read the actual upstream implementations, do not import
their code wholesale):

- [OpenCode `packages/core/src/tool/bash.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/bash.ts):
  typed command/workdir/timeout schema, PermissionV2, explicit local authority,
  structured exit/output/truncation, in-memory process output cap, active
  Location policy and controlled subprocess termination.
- [Claude Code Best BashTool](https://github.com/claude-code-best/claude-code/tree/main/packages/builtin-tools/src/tools/BashTool):
  AST-oriented command semantics and security, independent permission and
  path validation, dedicated local shell task lifecycle, background tasks.
- [DeerFlow sandbox contract](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/sandbox/sandbox.py):
  a backend-agnostic `execute_command` contract, per-scope execution,
  separately validated environment injections and provider-specific lifetime.
  [DeerFlow sandbox security](https://github.com/bytedance/deer-flow/blob/main/frontend/src/content/zh/harness/sandbox.mdx):
  explicitly identifies local shell as **host** authority, not a sandbox,
  and recommends isolation for untrusted agent tasks.

## Existing Ridge execution graph

```text
Agent model tools (per-step schema snapshot)
    -> ToolRuntime.prepare()
         -> mandatory core policy -> pre-hooks (owner thread)
    -> ToolRuntime.dispatch()
         -> ToolRegistry.execute_tool()
              -> sandbox/hosted allowlist + tenant membership
              -> discovery/mode guard + schema snapshot verification
              -> schema & input validation + circuit breaker
              -> adapter.run() (may execute on worker thread)
                 + Bash -> process_execution.run_process()
                 + Read/Edit/Write/Grep/Glob/LS -> workspace paths
                 + Team/Task/Skills/MCP -> own adapters
    -> ToolRuntime.publish()
         -> timing + post-hooks (owner thread)
    -> Agent events/history
```

`ToolScheduler` treats mutating/unknown tools as serial barriers; only
explicitly safe adjacent tools run in bounded parallel threads, with context
variables propagated and results published in model order. `ToolRegistry`
captures immutable per-step advertised schemas and denies revoked/stale
adapters. Worker registries copy adapters with separate noninteractive
permission gates and inherit hosted authority where present.

## Findings / priority

| Priority | Area | Current state | Decision |
| --- | --- | --- | --- |
| **P0 fixed here** | Bash output RAM | Former `communicate()` buffered arbitrary stdout/stderr and Bash returned full unbounded `data`, despite a `truncated` flag | Drain pipes into bounded buffers, count total bytes, cap returned lines, enforce limits *before* serialization |
| **P0 configuration requirement** | Local Bash authority | Workspace cwd and blacklists do **not** isolate files, processes or networking; arbitrary Python/scripts can access host capabilities | Document as trusted host execution only; require Docker/BoxLite/E2B/VM isolation for untrusted cloud agents; do not claim regex filters make this a sandbox |
| **P1 remaining** | Shell permission classification | `Bash._check_command_safety` is a regex/word heuristic, not a full shell grammar. It can be bypassed through interpreters, scripts and unexpected syntax, and may reject harmless text | Replace the role of regex-as-boundary with actual platform confinement and structured AST policy/approval; refuse uncertain commands when they require elevated authority |
| **P1 remaining** | Native Windows process tree | Current Windows fallback uses `taskkill /T /F`, not a Job Object | Introduce Job Object lifetime ownership, terminal process-group cancellation and Windows CI |
| **P1 remaining** | Long-running commands | One blocking Bash invocation (up to timeout), no persistent task/session or progressive output tool | Add scoped process sessions, TaskOutput/Stop wiring, TTL and explicit UI stream; ensure no orphaned tasks |
| **P1 remaining** | File operations | Read metadata and mtime+size optimistic checks exist; Write/Edit use temporary replacement, but race windows and symlink traversal remain; comparison by mtime+size is not content-hash CAS | Use fd-relative root policy, atomic replace with expected content hash, explicit file locks and concurrency tests |
| **P1 remaining** | Secrets in traces | Common `context.params_input` includes original tool args; exceptions may include data in error messages; stdout can include tokens | Central context-sensitive tool-param redaction plus bounded, access-controlled full logs; keep developer audit artifacts out of model prompts |
| **P2** | Schema ecosystem | `ToolParameter` + strict input registry, plus MCP schema validation; no unified enum/range/side-effect metadata on every tool | Move toward one typed `ToolDefinition` contract including capability, effect class, timeout and result policy |
| **P2** | Bootstrap registration | Builtins are imported/instantiated dynamically by filesystem scan; teams have special registration | Explicit feature/capability manifests, deterministic catalog order, optional deps preflight, startup diagnostics |
| **P2** | Search/read APIs | Dedicated `Read/Grep/Glob/LS`, 2s Grep deadline, bounded result/page options | Make result caps consistent, support `rg` missing fallback tests and binary/encoding handling |
| **P2** | MCP | Registry and MCP adapter exist with separate schema and protocol tests | Preserve adapter ownership and cancellation; standardize tool result errors and controlled resource cleanup |

## Design directions

1. **Execution ownership**: a tool definition is the public capability
   contract, not the OS executor. One Agent-session tool can bind a
   `CommandExecutor` that routes only through its selected sandbox provider
   or explicitly approved trusted host. No implicit fallback from VM to host.
2. **Effect classification**: `read`, `workspace_write`, `shell_exec`,
   `external_network`, `host_input` and `external_submit` represent
   separate permissions. A command may entail *several* effects; conservative
   classification/approval must happen before process spawn. Regex may
   supply hints, never confinement.
3. **Resource limits**: process time, memory, CPU, I/O and bounded retained
   output belong to the execution backend. Presentation-layer truncation
   happens again for model context, not as a substitute for backend caps.
4. **Concurrency**: serial mutations for same workspace; independent
   read-only calls may overlap. Process cancellation must propagate from
   user interrupt to every launched subprocess and descendant.
5. **Observability**: return a small stable `status/data/text/error/stats`
   result and trace status, duration, exit, bytes and truncation (no secrets).
   Any full logs should be opt-in artifacts with retention and ACLs, not
   implicitly copied into an LLM tool response.

## This PR's concrete changes

- `core/runtime/process_execution.py`: bounded capture/drain threads,
  per-stream byte counts and captured-output truncation, noninteractive
  stdin, POSIX process-group cancellation and timeout handling.
- `tools/builtin/bash.py`: inject the hard output limit, also cap returned
  lines and command size, reject invalid path/timeout input, make truncation
  visible and preserve original observed-byte statistics.
- `tests/test_bash_output_bounds.py`: large stdout/stderr flood, timeout,
  cancel, stdin, returned-data bounds, invalid inputs.
- All existing tool and sandbox policy tests continue to apply.

## Acceptance

CI must pass full Python regression, API smoke, Docker sandbox smoke, MCP
compatibility, Web UI and browser E2E. Changes in this PR are intentionally
limited to process/output safety and Bash contract corrections, **not** a
claim that an untrusted shell command is safe on the local host.

The separate draft PR #116 adds BrowserUse/ComputerUse; it is not included
in this branch and must be reviewed/merged independently.
