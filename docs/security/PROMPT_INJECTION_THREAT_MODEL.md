# Prompt-injection threat model and rollout (2026-10-20)

## Scope

Ridge Code is a coding agent with access to shell commands, project files,
Skills, MCP tools, hooks, and session history. A hostile repository, tool
result, MCP server, Skill, website, or restored session may carry instructions
that attempt to impersonate the user, application, or system. The model must
not decide its own authority based on what untrusted text says.

**Do not equate prompt framing with a security sandbox.** The core tool
permission checker, real process isolation, network egress policy, and
credential boundaries must deny unsafe actions independently of model output.

## Trust levels

| Source | Model presentation | Enforcement owner |
| --- | --- | --- |
| Bundled L1 prompt / application policy | System, trusted application code | Application install integrity |
| Actual user request | User | Authenticated session |
| CODE_LAW / repo docs / Skill descriptions | Escaped user-role reference data | Registry and tool-level permissions |
| MCP metadata / hook context | Escaped user-role reference data | MCP tool validation and hook trust policy |
| Tool results / web text | Escaped tool-role data | Tool registry and sandbox |
| Persisted conversation snapshot | History only; former system messages ignored | Session loader and authenticated binding |

All externally controlled text is *data*, even if it contains words like
"system", "developer", "security policy", or claims that the user authorized
a destructive or network operation. Framing includes provenance and escaped
tags to avoid a simple closing-tag breakout. This is guidance for the model,
not a proof that the model will reject every attack.

## Direct fixes in this change

1. Load bundled `L1_system_prompt.md` as bounded static text, rather
   than executing an arbitrary `L1_system_prompt.py` with `runpy.run_path`.
   Resolve the Markdown file from the installed application; a trusted
   bootstrap/test fixture may supply an explicit root. Never load L1
   from the active repository merely because it contains a prompt file.
2. Remove CODE_LAW, MCP descriptions, Skill metadata and hook/runtime text
   from `system` messages. Present them as provenance-labelled, escaped,
   bounded reference text in `user` messages before conversation history.
3. Frame the current model projection of `tool` messages without changing
   the stored history or tool-call IDs.
4. Ignore serialized `system_messages` on restored sessions rather than
   turning disk content into fresh system instructions.
5. Add adversarial tests for delimiter breakout, role promotion, snapshot
   poisoning, unexpected Python execution and history mutation.

## Execution-layer follow-up (2026-10-20)

- `core/runtime/subprocess_environment.py` defines a conservative ambient
  environment allowlist. Local Bash gets a temporary HOME, so `OPENAI_API_KEY`,
  AWS tokens, `GITHUB_TOKEN`, dynamic loader hooks, `PYTHONPATH`, `NODE_OPTIONS`,
  injected Git config variables, and authenticated proxy variables are not
  automatically inherited. MCP stdio always receives a similarly stripped
  environment; a server's **explicit** trusted `env` config is treated as a
  deliberate credential grant, never as permission inferred from model output.
- Docker and BoxLite re-scan bind-mounted workspace trees for recognizable
  credential files/directories at creation and before command execution.
  They reject full RW mounts containing `.env`, `.ssh`, `.aws`, `.npmrc`,
  private keys and similar names; public `.env.example` templates are allowed.
  An extremely large or unreadable tree is denied. This is a narrow preflight,
  not a secure copy or race-free filesystem boundary. Exclude secrets from a
  separately prepared sandbox workspace **before** running the Agent.
- `AGENT_REQUIRE_ISOLATED_EXECUTION=true` makes an attempt to start an Agent
  with `local_trusted` fail before tool wiring. Set `AGENT_SANDBOX_MODE=docker`,
  `boxlite`, or `e2b` with an approved backend. No host-mode fallback occurs.

**Production note:** Unrestricted host Python/build tools can still use direct
sockets and read user-accessible files; `BASH_ALLOW_NETWORK=false` is only a
command-name filter. No amount of environment filtering turns host Bash into
network isolation or an OS sandbox. A real isolated backend is required when
repository contents or tool results are untrusted.

- MCP trust flags and the `MCP_SERVERS` override must originate in the
  operator's process environment; keys newly introduced by an automatically
  loaded workspace `.env` never authorize MCP execution.
- Repo-provided `mcp_servers.json`, `.mcp.json`, and `mcp.json` now
  require `MCP_TRUST_PROJECT_CONFIG=true` before any project server is loaded.
  A backend-set `MCP_SERVERS` JSON is the out-of-repository operator path.
  Background/manual MCP connection can execute stdio commands: inspect
  commands, args, explicit environment credentials, destinations and
  server update mechanisms before opting in.
- Trusted command hooks now use the same minimal inherited subprocess environment.
  A hook's explicit trust is not an implicit grant for unrelated model/cloud
  credentials. Hooks nevertheless run on the host with the operator's OS
  filesystem/network privileges and are not safe in a malicious workspace.

- Local trusted Bash can now use `BASH_COMMAND_APPROVAL=ask` to require
  exact, untruncated command confirmation through the authenticated session
  permission broker before execution. `deny` blocks host Bash outright;
  `allow` is the compatibility default for intentionally trusted local
  workspaces. Without a UI broker, `ask` fails closed. Commands above 4096
  characters fail closed in approval mode rather than displaying a truncated
  authorization request. This is a second-line confirmation and does NOT
  constrain spawned processes, unapproved file access by other tools or
  unauthorized network sockets. For untrusted repos use isolated execution.

## Additional enforcement and context fixes (2026-10-20)

- Dynamically registered tool `usage_notes`, temporary disabled-tool
  listings and output-style text are **no longer interpolated into L1 system
  instructions**. Even a plugin that advertises a tool can only contribute a
  framed, escaped reference. Bundled L1 and the application trust-boundary
  policy retain their separate trusted priority.
- Serialized history entries claiming `system` or `developer` roles are
  projected as untrusted data in the model request. The saved session itself
  is not mutated. This blocks a second role-promotion route beyond the former
  `system_messages` snapshot field.
- `core/runtime/process_execution.py` drains stdout/stderr concurrently,
  retaining at most 128 KiB **per stream** by default to fit base64-encoded,
  64 KiB guest filesystem transfer responses. Host Bash explicitly requests a
  tighter 50 KiB per-stream cap, and sandbox Bash separately presents at most
  50 KiB of each stream to the model. Overflow flags propagate through result
  envelopes. This addresses capture-memory exhaustion, **not** CPU usage,
  IO rates, open files or all descendants; Windows descendant termination
  still requires a Job Object.
- The dedicated security workflow now includes stream-overflow, child
  cancellation and Bash behavior regressions alongside prompt-injection tests.

## Remaining risks / mandatory production gates

- **Host command execution:** `tools/builtin/bash.py` uses shell parsing
  and command-name blocklists. Parsers cannot reliably constrain subprocesses
  started by Python, build scripts, package managers, or indirect tooling.
  A command can also read accessible files or access the network without
  invoking curl/wget. Untrusted repositories must run inside a locked-down
  Docker/VM/OS sandbox with read-only sensitive mounts, no inherited secrets,
  explicit network egress policy, user namespaces and resource quotas.
- **Action authorization:** review Bash, file writes, external destinations,
  MCP, browser/computer use and plugin hooks before side effects. Implement
  deny/ask/allow as an operation-scoped, server-enforced, immutable decision.
  Revalidate immediately before execution and prevent TOCTOU grants and
  subprocess bypass; logs and model compliance are not authorization.
- **MCP schema and tool descriptions:** their text may appear in separate
  provider-side tool metadata, so the model can still be influenced. Tool
  allowlists, endpoint trust and runtime approval remain required.
- **Skill supply chain:** check contents of SKILL.md and bundled resources
  for override instructions, malicious scripts, symlinks, secrets, zip-bombs,
  path escapes, dangerous binaries and network destinations before enabling.
  A plugin hash alone is not a publisher signature.
- **Data egress:** prevent reads of token/key files and cloud metadata, strip
  secrets from child environments, and restrict DNS/HTTP and other outbound
  protocols in the sandbox. Trace redaction does not stop a running process.
- **Persistent poisoning:** validate imported sessions, compaction summaries,
  memory, generated team output, browser content and documents as untrusted
  sources on all model-entry paths. The framing implemented here covers the
  normal context-builder tool-message path, not every downstream consumer.
- **Evaluation:** run automated adversarial tests with real models, files,
  MCP servers and sandbox workloads. CI unit tests are not an end-to-end
  adversarial robustness guarantee.

## Verification

```bash
python -m pytest tests/test_context_builder.py tests/test_prompt_injection_boundaries.py -q
python -m pytest tests/test_tool_runtime_pipeline.py tests/test_permission_gate.py -q
python -m ridgecode doctor --target core
```

Recommended additional live exercises: malicious CODE_LAW and Skill headers,
MCP result claiming system authority, web-page tool-output injection, encoded
role delimiter, a poisoned serialized session, attempted `.env` exfiltration,
a build script spawning a new child process, and cross-workspace file writes.

## References (design inspiration, not claims of identical security guarantees)

- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [DeerFlow prompt sanitization and SkillScan updates](https://github.com/bytedance/deer-flow/discussions/5832)
- [Claude Code Best: sandbox as second defence layer](https://github.com/claude-code-best/claude-code/blob/main/docs/safety/sandbox.mdx)
- [OpenCode permission model](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/permissions.mdx)
- [OpenCode security threat model: permissions are not a sandbox](https://github.com/anomalyco/opencode/blob/dev/SECURITY.md)
