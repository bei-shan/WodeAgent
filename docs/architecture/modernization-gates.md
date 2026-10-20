# Ridge Code architecture modernization gates

This is the living review checklist for the current `main` implementation. Historical
`docs/design/` ADRs describe decisions at their original date; they are **not**
proof that their older `AgentFeature` runtime still exists.

## Canonical ownership (no legacy AgentFeature)

| Concern | Sole owner | Do not reintroduce |
| --- | --- | --- |
| Service lifecycle, MCP, sandbox, teams, session | `core/runtime/services.py` and `service_*.py` | `core/features/`, `AgentFeature`, numeric lifecycle order |
| Non-optional plan/budget/delegation guards | `core/runtime/policy_middleware.py` | Plugins with the ability to disable security policy |
| Tool discovery/authorization/dispatch | `tools/registry.py` and `core/runtime/tool_runtime.py` | Model-visible schema as permission proof |
| User extensions | `core/plugin_loader.py` and `core/skills/` | Arbitrary Python `custom_features` |
| TUI | `tui/` with `python -m ridgecode tui` | `tui-v2/` or retired Python terminal |
| Context projection/compaction | `core/context_engine/` and `core/runtime/context_runtime.py` | Per-feature prompt rewriting |
| Hosted identity and durable jobs | `desktop/service/hosted_*.py`, `core/persistence/` | Treating local-trusted API as multi-tenant |

Feature *flags* in upstream source and archived discussions are a separate concept;
they do not justify reintroducing the removed `AgentFeature` extension ABI.

## Ordered acceptance gates

| Priority | Topic | Verification required |
| --- | --- | --- |
| P0 | Prompt and repository injection | Untrusted repository instructions never execute Python during prompt assembly, escalate priority, or bypass tool approval; run adversarial fixtures |
| P0 | Tool output/artifact containment | Output names cannot escape directories, symlinked paths cannot redirect secret receipts, file mode is private, concurrent receipts do not collide; enforce per-session isolation |
| P0 | Execution authority | Denied and stale tool calls have zero side effects; nested MCP/subagents cannot gain additional rights; cancellation invalidates approvals |
| P1 | Context budget/compaction | Count full request (system, schemas, history, files and modality-dependent budget); preserve complete tool-call/result pairs; test multiple compactions and fork restore |
| P1 | Reliable model cancellation | Bound timeout workers and request resources; prove provider cancellation does not leak running background threads |
| P1 | Durable working state | Preserve task objective, plan, touched files, test evidence, blockers, and subagent state independently of lossy summary text |
| P1 | Browser/computer/file tools | Prove real end-to-end sandbox-scoped sessions, attachment and artifact authorization, platform-specific cancellation |
| P2 | Memory/providers | Authenticated scopes, selection, revocation and retention; explicit opt-in, source provenance, quality tests |
| P2 | Hosted execution | Verified principal end-to-end, durable execution lease/fencing, isolated workspace/credentials, SSE replay and worker recovery |
| P2 | Release | Reproducible exact-SHA CI; live Docker/VM/cloud integration, staged cross-tenant attacks, long-running load, operator sign-off |

## Test commands and status rules

```bash
python -m pytest tests/test_repository_hygiene.py tests/test_runtime_v2_no_legacy.py tests/test_retired_tui_boundary.py -q
python -m pytest tests/test_context_continuity.py tests/test_branch_compaction_isolation.py tests/test_observation_truncator.py -q
python -m pytest tests/test_tool_runtime_pipeline.py tests/test_tool_scheduler.py -q
cd tui && bun install --frozen-lockfile && bun run typecheck && bun test
```

A test is **PASS** only with an actual run URL/log and release commit SHA. Mark
missing prerequisites **BLOCKED**, never PASS. For full release criteria consult
`docs/releases/production-gates.md`; hosted multi-tenant Agent execution remains
**NO-GO** until the documented gates are independently verified.

Reference implementations for design comparison (patterns, not code copies):
[OpenCode](https://github.com/anomalyco/opencode),
[DeerFlow](https://github.com/bytedance/deer-flow), and
[Claude Code Best](https://github.com/claude-code-best/claude-code)
(the last is a third-party project, not Anthropic's official source).
