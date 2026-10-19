# Reference audit hardening Implementation Plan

> **For agentic workers:** Use subagent-driven-development with task-scoped implementers and independent reviews. Do not edit the original main checkout while implementation is in progress.

**Goal:** Repair false task completion, isolate worker tool bindings and remove the confirmed frontend dependency findings.

**Architecture:** Keep the current AgentLoop/TurnExecutor and canonical outcome pipeline. Use structured failure kinds, explicit final-result acceptance, isolated adapter bindings and patched frontend build dependencies.

**Tech Stack:** Python 3.12, pytest, React/TypeScript, Vite, Tailwind/PostCSS, GitHub Actions.

## Global Constraints

- Do not start or deploy any service on the Tencent server; preserve CityAIHub.
- Do not use paid model calls or real user data for tests.
- Keep changes inside the isolated managed worktree until review and validation pass.
- Preserve public interfaces and providers that omit finish reasons.
- Never classify truncated output, empty termination or a final tool observation as task success.
- Preserve sandbox seals, hosted authority fences and parent tool permissions.
- Do not deep-copy or independently close shared MCP transports.
- Record benchmark modes accurately; scripted-model passes are not real-model capability scores.
- Do not claim public multi-tenant production readiness from these repairs.

## Task 1: Honest terminal outcomes

Files: `core/llm_recovery.py`, `core/response_parser.py` if required, `agents/codeAgent.py`, `core/runtime/agent_loop.py`, `core/runtime/turn_lifecycle.py`, `core/team_engine/turn_executor.py`, `core/team_engine/execution.py`, `tools/builtin/task.py` runner failure handling, and focused tests.

- [x] Add red tests using real scripted-provider CodeAgent + AgentSession for length-capped text and length-capped tool calls, two empty replies before the step budget, and valid stop/omitted finish reasons.
- [x] Add red tests for team work-item exhaustion and tool-error-then-empty replies; assert persisted failure and no completed board task. Keep a tool-error-then-valid-final positive control.
- [x] Extend the existing `ModelInvocationError` taxonomy with explicit incomplete-output and empty-response kinds; retain private partial evidence without converting it to a successful final.
- [x] Guard explicitly incomplete termination before tools can execute in both CodeAgent and TurnExecutor. Reserve `_run_limit_hit` for actual loop exhaustion.
- [x] Require a nonempty model final in `ExecutionService`; preserve the observation trail as diagnosis, and avoid automatic whole-task replay after side effects.
- [x] Ensure synchronous/background Task runners propagate model/empty/step-limit failures rather than wrapping an error string as a completed task.
- [x] Run loop/gateway/executor/lifecycle/recovery/team tests, inspect the diff and pass independent task review.

Expected failure contract is equivalent to:

```python
with pytest.raises(ModelInvocationError) as caught:
    agent.run("complete the task")
assert caught.value.kind == "output_truncated"
assert agent.last_turn_status == "failed"
assert not any(event.type == "assistant.final" for event in captured_events)
```

## Task 2: Independent worker tool bindings

Files: `core/team_engine/execution.py`, `core/team_engine/turn_executor.py`, `tools/builtin/task.py`, `tools/registry.py` or a shared binding helper if warranted, and focused permission/task/team tests. Execute after Task 1 because the runtime files are shared.

- [x] Reproduce parent adapter/gate mutation for both derived-registry paths and teammate access to parent-only controls.
- [x] Clone only the adapter binding before replacing a permission gate. Preserve shared transport/service references, authority restrictions and explicit allow/deny filters.
- [x] Exclude or safely bind main-agent-only interaction, plan/workspace and discovery controls; never retain a callable path that changes the parent Agent's mode or workspace.
- [x] Verify parent adapter identity, broker, interaction callback and behavior remain unchanged after worker registry creation and execution.
- [x] Verify allowed worker file tools and shared MCP adapters still function, and rejected capabilities remain rejected.
- [x] Preserve hosted membership/allowlist and sandbox restrictions plus mode/discovery decisions in every derived registry; verify revocation after derivation cannot execute a marker adapter.
- [x] Capture a fresh advertised schema/snapshot before each model call and route execution through canonical `ToolRegistry.execute_tool`, including its dynamic, stale-schema and strict-input gates.
- [x] Run permission/Task/team/snapshot/hosted/sandbox regressions and independent task review.

## Task 3: Patched frontend build dependencies

Files: `desktop/web/package.json`, `desktop/web/package-lock.json`, PostCSS/CSS integration, migration-sensitive class sites and browser tests where needed.

- [ ] Save the existing generated CSS and run baseline build before changing dependencies.
- [ ] Pin the verified compatible patched dependency versions from the design. Update the lockfile through npm without force overrides that hide findings.
- [ ] Replace the Tailwind PostCSS integration, explicitly load the existing theme and preserve renamed shadow/radius/outline and spacing behavior.
- [ ] Validate generated/computed CSS for the existing layout; run browser regressions, including security-sensitive Markdown rendering.
- [ ] Run `npm ci`, `npm run build`, `npm audit --json` and `npm audit --omit=dev --json`; require zero findings and record browser floors.
- [ ] Pass independent task review.

## Task 4: Comparison record and delivery

- [x] Freeze the three reference revisions and save inspected source outside the repository.
- [x] Record the rejected hook-mutation hypothesis and passing boundary probes.
- [ ] Update the audit record with source evidence, repaired defects, checked-safe boundaries and outstanding production gates.
- [ ] Run combined regression/eval and independent whole-branch review.
- [ ] Push a repair branch, create/attach a PR, and verify exact-head Linux full/browser/Docker/MCP/TUI/docs CI.
- [ ] Merge only the approved green head, fast-forward local main and independently compare remote SHA/file tree.
- [ ] Run feasible offline benchmark checks on final main and report their limits.

Baseline: the isolated `0756114` checkout passed 54 focused loop/executor/lifecycle/recovery/policy/tool-contract tests. Known Windows POSIX-only failures are not repaired by weakening boundaries; Linux CI validates those paths.
