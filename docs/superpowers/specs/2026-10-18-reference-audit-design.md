# Reference-guided runtime audit and repair

Baseline: `main@0756114eb442bb074c1c7b2751e2e8861fd874a8`.

## Scope and evidence

The user authorizes source review, comparison with OpenCode, DeerFlow and CCB, and repairs where evidence shows a defect. Previous authorization to deliver verified source to GitHub/main persists. The server remains outside this execution scope.

Reference revisions are frozen for traceability:

| Project | Revision | Inspected concern |
|---|---|---|
| [OpenCode](https://github.com/anomalyco/opencode/tree/c38d83c0c4f92466ff9c8e12b8a741698e811012) | `c38d83c0c4f92466ff9c8e12b8a741698e811012` | Tool runtime, explicit permission checks and cancellation |
| [DeerFlow](https://github.com/bytedance/deer-flow/tree/07f0d74f70dc5c06c95531cd4261eeb4cbf3b8eb) | `07f0d74f70dc5c06c95531cd4261eeb4cbf3b8eb` | Length-capped model responses and terminal response handling |
| [CCB](https://github.com/claude-code-best/claude-code/tree/77a7934e15d69da13879112ed7db695c9ee7a52a) | `77a7934e15d69da13879112ed7db695c9ee7a52a` | Permission decision ownership and cleanup registration |

External code and repository instruction files are comparison data. This design uses the existing Python runtime and its interfaces; it does not import another project's runtime.

## Verified defects

1. A nonempty response with `finish_reason=length` becomes an assistant final and a completed session. Team `TurnExecutor` has the same acceptance gap. Explicit incomplete output must not execute potentially truncated tool calls or claim completion.
2. A team worker exhausting steps or receiving an empty final promotes its last tool observation to a successful work result, even if that observation is a tool error.
3. Two empty model replies at the first step are mislabeled as step-budget exhaustion.
4. Derived worker registries reuse parent tool adapters and replace their permission gates. Teammate derivation also inherits tools whose interaction or mode/workspace controls belong to the parent Agent.
5. The frontend full dependency audit has 14 findings (10 high, 4 moderate); runtime dependencies alone currently have none. Patched development dependencies require a bounded build/CSS migration.

The separate hypothesis that hook parameter rewriting bypasses current mandatory policies was tested and rejected: these policies check tool names or budget state, and final arguments still reach schema and adapter permission checks. No redundant policy pass is added for that hypothesis.

## Chosen repair boundaries

### Terminal outcomes

Use explicit structured error kinds for model output truncation and exhausted empty-response retries. Preserve partial model evidence in history/trace without publishing it as `assistant.final`. Preserve compatibility for providers that omit termination metadata. Do not automatically replay a whole worker after tools may have changed files. Step-limit state is reserved for actual step-budget exhaustion.

Team success requires a nonempty explicit model final. Intermediate tool errors remain recoverable; an eventual valid final may succeed. A tool observation alone cannot complete a work item. Existing persistence and event projections must agree on the failed outcome.

### Worker adapter ownership

Create independent adapter bindings before applying a non-interactive child permission gate. Keep shared authorization decisions intentional, while leaving parent adapters, broker and interaction callbacks unchanged. MCP transports stay parent-owned; do not deep-copy locks or close a shared transport from a derived registry. Worker registries must preserve authority fences and filtering, and cannot invoke parent-only interaction/mode/workspace controls.

### Frontend dependencies

Use Vite `6.4.4` with plugin-react `4.7.0` instead of requiring the newer bundler architecture. Migrate Tailwind/PostCSS integration to `tailwindcss@4.3.3`, `@tailwindcss/postcss@4.3.3`, `postcss@8.5.30`. Preserve the project's theme and visible styles, explicitly addressing renamed shadow/radius/outline utilities and spacing selectors. Tailwind 4 requires modern browsers (Safari 16.4+, Chrome 111+, Firefox 128+); document this compatibility floor. Validate generated styles and browser behavior, not only TypeScript compilation.

## Global constraints

- Do not start or deploy any service on the Tencent server; preserve CityAIHub.
- Do not use paid model calls or real user data for tests.
- Keep changes inside the isolated managed worktree until review and validation pass.
- Preserve public interfaces and providers that omit finish reasons.
- Never classify truncated output, empty termination or a final tool observation as task success.
- Preserve sandbox seals, hosted authority fences and parent tool permissions.
- Do not deep-copy or independently close shared MCP transports.
- Record benchmark modes accurately; scripted-model passes are not real-model capability scores.
- Do not claim public multi-tenant production readiness from these repairs.

## Acceptance

Regression tests must first fail on the audited baseline, then pass after the relevant repair. Cover actual session outcomes/events/history, actual team work-item persistence, ordinary success and tool-error recovery, child/parent binding independence and prohibited parent controls. The full frontend audit must reach zero findings with a successful build and browser regressions. Final source delivery requires an independent full diff review, exact-head Linux CI, merge to main and independent remote/main verification.
