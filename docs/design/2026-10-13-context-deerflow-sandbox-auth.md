# Context Engineering v3 & DeerFlow Sandbox/Auth Review

Date: 2026-10-13  
Scope: WodeAgent context reliability now; sandbox and identity design review for future PRs.

## Sources (reviewed, not copied)

- [DeerFlow summarization middleware](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py), [durable context middleware](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py), [tool output budgeting](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/agents/middlewares/tool_output_budget_middleware.py)
- [DeerFlow sandbox interface](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/sandbox/sandbox.py), [provider](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/sandbox/sandbox_provider.py), [sandbox authorization gate](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/sandbox/middleware.py)
- [DeerFlow authentication middleware](https://github.com/bytedance/deer-flow/blob/main/backend/app/gateway/auth_middleware.py), [CSRF middleware](https://github.com/bytedance/deer-flow/blob/main/backend/app/gateway/csrf_middleware.py), [architecture guide](https://github.com/bytedance/deer-flow/blob/main/docs/ARCHITECTURE.md)
- [OpenCode compaction](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/compaction.ts) and [session context](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/context.ts)
- [Claude Code Best autoCompact](https://github.com/claude-code-best/claude-code/blob/main/src/services/compact/autoCompact.ts), [microCompact](https://github.com/claude-code-best/claude-code/blob/main/src/services/compact/microCompact.ts), [compact prompt](https://github.com/claude-code-best/claude-code/blob/main/src/services/compact/prompt.ts) (**third-party reconstruction, not Anthropic source**)

All designs are independently reimplemented in Python. No upstream source code is copied.

## 1. Context mechanisms compared

| Mechanism | Reference | WodeAgent approach |
| --- | --- | --- |
| Concise rolling checkpoint + complete recent exchanges | OpenCode `session/compaction.ts` | Keep one updated `summary` message, fold old summary back into next compaction, retain last N complete user rounds and their tool-call/result pairs |
| User intent / decisions / work state / files / next move | OpenCode summary template; Claude Code Best compact prompt | Updated `summary_prompt.py`, limited durable original-user-goal anchor and recent user constraints from archived messages |
| Active task context with provenance | DeerFlow durable context middleware | Anchors are **untrusted historical user data** inside a user-level summary, XML-escaped and bounded; never injected as system authority |
| Bounded compaction requests and output | DeerFlow summarization; OpenCode compaction budget | Configurable `SUMMARY_INPUT_MAX_CHARS`, `SUMMARY_OUTPUT_MAX_CHARS`, `CONTEXT_ANCHOR_MAX_CHARS`; preserve oldest goal + newest progress on oversized source |
| Headroom for generated answer and tool output | OpenCode buffer; Claude Code Best autocompact thresholds | `CONTEXT_RESPONSE_RESERVE_TOKENS`, clamped for very small windows; compress before provider overflow |
| Avoid repetitive failed compaction | Claude Code Best max consecutive failures | `CONTEXT_COMPACTION_MAX_FAILURES` (default 3), reset on next user request and on successful compaction |
| Lossless failure | All three projects favor explicit failure over silent context corruption | On missing/error/blank/oversize summary, leave complete history + cursor untouched; do not truncate old turns and call it a successful summary |
| Cheap old-tool result trimming | Claude Code Best microCompact; DeerFlow ToolOutputBudget | **Already implemented** `observation_truncator.py`; future phase: request-only trimming of stale tool payloads while keeping original receipts/snapshots |

### This PR's changes and acceptance tests

`core/context_engine/continuity.py` (new), `history_manager.py`, `summary_compressor.py`, `core/config.py`, `prompts/agents_prompts/summary_prompt.py` with tests in `test_context_continuity.py` plus existing context tests.

- Preserve original objective over 2+ compactions including `serialize_entries` / `load_entries` restart.
- Keep newest complete rounds and valid OpenAI assistant `tool_calls` + matching `tool` responses.
- Skip destructive compaction on generator outage, timeout, malformed/oversized summary; bounded retries.
- Summary prompt preserves unfinished work, decisions, exact paths, pending verification.
- Note: summary precision depends on the model. Tests use mock LLM; **no claim of factual/semantic perfect summaries**.
- A raw user statement may contain secrets: compaction history and resulting summary remain local session data; privacy/sanitization deserves a separate follow-up.

## 2. DeerFlow sandbox: what to integrate (not yet implemented)

### What DeerFlow actually does

DeerFlow separates a `Sandbox` tool contract (command, read/write/list/glob/grep) from `SandboxProvider` lifecycle (`acquire / get / get_scoped / release`). A sandbox is bound to **user and thread identity**, and may use container-backed providers. Its local provider **executes on the host and is not a security boundary**; DeerFlow's `sandbox/security.py` denies host Bash unless explicitly allowed for trusted local operation. Authorization occurs **before both reusing and acquiring** a sandbox; provider isolation capability is checked when skill files are shared. The lease manager prevents release while concurrent agents/tools still hold the sandbox, and remote command scope makes subagents' shell state explicit.

### Current WodeAgent gap

The current `tools/permission_gate.py` is a **soft filesystem access permission gate**; `_rebind_project_root` plus a per-session workspace is not an OS/container isolation guarantee. A subprocess can potentially access host resources or network beyond that workspace. Also: real-time filesystem UI and tool registry have no per-user owner scope. Never advertise them as secure isolation.

### Recommended staged architecture

1. **P0 (separate PR):** `SandboxProvider` interface, `LocalTrustedProvider` explicitly labeled unsafe and only enabled via trusted-local config; future `DockerSandboxProvider` runs unprivileged as non-root, with no privileged flags, host Docker socket, root filesystem mounts, or inherited API keys. Shell, file, skills, MCP and subagent operations must route through the same provider tool boundary; otherwise isolation is bypassed.
2. **P0:** per-session sandbox scope + lifecycle lease/ref-count; deterministic teardown on cancellation, timeout and deletion, and startup reconciliation of orphan resources. Identity `(owner_id, session_id)` must be server-created; reject caller-supplied sandbox IDs.
3. **P1:** Docker-only path mapping into a dedicated workspace volume; read-only skill/config mounts, controlled writes and temporary storage quotas; limits on CPU, memory, processes, file size and execution timeout. Network **default deny** with explicit per-tool approval / narrow allowlist, no implicit host credentials, prevent Docker host gateway escape. Validate symlinks and path traversal inside provider filesystem boundary.
4. **P1:** integration tests attempt reading host `~/.ssh`, `/etc`, Docker socket and another user's workspace; test network egress, process/timeouts and cleanup after abort. A path-prefix restriction alone does **not** pass these tests.

**Adoption decision:** Borrow provider contracts and threat model; do **not** bulk-copy DeerFlow's LangGraph runtime or complex distributed provisioner. Start with one validated single-machine container backend. Keep LocalTrusted only for developer-controlled localhost use.

## 3. DeerFlow auth/authz: what to integrate (not yet implemented)

DeerFlow's Gateway layers authentication, route/resource authorization, CSRF/origin validation, user identity propagation, and scoped sandbox execution. WodeAgent currently allows unauthenticated REST/WS on localhost and has no user-owned sessions or CSRF.

Recommendation (separate security PR):

1. Decide **single-user local** vs **remote/multi-user** deployment. For local desktop, an unpredictable boot/session token with strict loopback Host/Origin checks and WebSocket handshake validation may be enough; it is **not** a multi-user access-control model. Do not use an exposed fixed password in browser JavaScript.
2. For remote use, run behind TLS and real session authentication (secure HttpOnly SameSite cookies, short-lived session, login/logout and CSRF/origin checks), or federated OIDC. Public endpoints should be narrowly explicit. Rate-limit login and uploads; never log tokens.
3. Create server-resolved `principal.user_id` and ownership checks on **every** session REST/WS/file/history/upload/tool/interrupt/config and sandbox acquisition, not only login. Session ID randomization alone is not authorization. Impersonation and untrusted user-ID headers must fail closed.
4. Gate high-risk tools (shell, file writes, network, MCP access, workspace mounts) by principal + requested action with auditable approval. Denial must remain denied on errors; no global decision cache across users. Approvals should be linked to explicit path/tool/scope and expire.
5. Test cross-user 403, malformed/expired cookie, CSRF, WebSocket origin/cookie, untrusted host headers, DNS rebinding, upload/symlink traversal, expired sessions and sandbox reuse after role changes.

**Current safety posture:** local trusted MVP only. Do not bind publicly on `0.0.0.0` or put the API behind a public reverse proxy until this security PR is implemented and verified.

## 4. Sequencing and merge order

- PR #2 event replay → PR #3 turn semantics and safe LLM recovery → **this context PR** (stacked on #3). Merge in that order and retarget each stacked PR to `main` after its predecessor.
- Next **sandbox contract + opt-in container provider**, separate from **authentication/authorization**; neither is safe to ship half-integrated with only HTTP middleware.
- Later: source-backed context records with citations and stale tool-result elision in the outgoing model request only.
