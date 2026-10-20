# Command security, process containment, file races and attachments

Status: draft implementation (2026-10-20) on PR #117. Does not assert
multi-tenant production acceptance without external security review.

## References and adopted boundaries

- [OpenCode attachment interface](https://dev.opencode.ai/v2/docs/attachments/):
  typed attachment media, model-compatible images, text resources, explicit
  size caps; unknown media is not silently interpreted as an image.
- [DeerFlow upload design](https://github.com/bytedance/deer-flow/blob/main/backend/docs/FILE_UPLOAD.md):
  session/thread-rooted uploads, separately bounded conversion, virtual
  sandbox paths and temporary media, upload limits/cross-session isolation.
- [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects):
  native process tree ownership, kill-on-close. Ridge's Windows CI exercises
  a grandchild that attempts to survive after command timeout.
- [CCB Bash design](https://github.com/claude-code-best/claude-code/tree/main/packages/builtin-tools/src/tools/BashTool):
  command permission checks kept distinct from OS process execution.

## What is implemented

### 1. Bash exact-command approval

`core/runtime/command_approval.py` classifies command effects conservatively:
`read`, `workspace_write`, `external_network`, `shell_exec`.
Only a minimal safe set of simple commands is non-interactive. Unknown
commands, interpreters, pipelines, redirections and compound shells require
approval. Session-bound Web `PermissionGate._broker` handles a request for
the **specific exact command**, including a digest. Consent is not cached
by path. The model cannot set an effect class through tool args.

Local CLI/headless with no broker may still use trusted-host Bash by default
for compatibility. `RIDGE_BASH_APPROVAL_MODE=strict` instead denies
approval-required commands when a broker is unavailable. Approval is
**NOT** confinement; shell syntax and arbitrary script contents must still
execute in isolated workspaces for untrusted agents.

### 2. Sandbox vs host process isolation

Existing Docker, BoxLite and E2B providers are separate Bash adapters
(`SandboxBashTool`). Docker enforces `--network=none`,
`--cap-drop=ALL`, `--read-only`, `--security-opt=no-new-privileges`,
memory, CPU and PID caps, with guest-only process execution. It explicitly
refuses host fallback when the provider fails. This remains the recommended
production execution route; Docker and BoxLite are not a substitute for
reviewing mount authority and cloud tenant assignment.

**New**: `RIDGE_BASH_REQUIRE_SANDBOX=true` disables the ordinary local
`BashTool` completely, including read-only shell commands, while leaving
`SandboxBashTool` enabled. This makes a misconfigured deployment fail closed
instead of running unknown commands on the web server host.

### 3. Windows Job Object

`core/runtime/windows_job.py` binds a new Job Object to each local Windows
process, with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Critical ordering:
the process is created with `CREATE_SUSPENDED`, assigned to the job,
then the initial Win32 thread is resumed. This removes the known fast-child
race from Popen-followed-by-Assign: an unsuspended parent can already have
created a grandchild outside the job before assignment.

Failure to assign/resume rejects execution, never downgrades to an
unowned process. `.github/workflows/windows-job-object.yml` performs a
real Windows nested-process timeout test. The Job is process ownership,
**not** privilege, filesystem or network isolation.

### 4. File system publication

`core/runtime/file_commit.py` now accepts `workspace_root` for all
Write/Edit/MultiEdit tools. When POSIX `dir_fd`, `O_NOFOLLOW`, and
`flock` are available, it opens each parent directory from the pinned root
fd, preventing parent-symlink swaps from redirecting tool writes outside the
workspace. Commits are same-directory and atomic; cooperative local tools
serialize on a directory lock; modes are preserved. Existing-file identity
is rechecked (inode, device, nanosecond mtime/ctime, size).
New files are linked exclusively without overwriting concurrently created
files.

**Remaining limitation:** An unrelated process that ignores advisory locks
can still race the final existing-file compare/rename; true atomic
compare-and-swap against arbitrary writers needs a stronger storage/overlay
transaction model. Out-of-workspace explicitly approved paths and Windows
fallback still need equivalent handle-scoped protection.

### 5. Attachment pipeline

```text
ChatComposer selected files
   -> POST /sessions/<id>/upload       (existing streamed 25 MiB cap,
                                       O_NOFOLLOW + exclusive file creation)
   -> POST /sessions/<id>/messages
      { content, attachments: ["session-relative-file"] }
   -> WorkspaceService.prepare_attachments()
      (verify session, descriptor-pinned open, max 4 parts,
       8 MiB combined, 4 MiB/image, 96 KiB/text)
   -> AgentSession private one-turn media (memory only)
   -> AgentLoop first model invocation
      * magic-sniffed PNG / JPEG / GIF / WebP: image_url data part
      * UTF-8 source/text files: text part
      * PDF / Office / unknown binary: path-only reference
   -> model (provider/model must support image input)
   -> history, traces, session snapshot: text + filenames only
   -> clear transient media on turn completion/error
```

The client never controls an absolute path, a URL, arbitrary Base64 or MIME
type for model injection. Attachment paths must resolve through the active
session's fd-confined workspace, not an arbitrary host directory. The
provider may reject image input if its model lacks multimodal support;
we do not silently claim the pixels were processed.

Raw PDF/Office and unknown media currently remain **path-only**. They are
available inside the session workspace for an explicitly scoped parser/tool,
but this PR does not add a privileged PDF/Office conversion worker.
Production support should put PDF/Office parsing behind isolated,
resource-capped conversion and return a bounded text/Markdown derivative.
No archive should be extracted inside the API worker.

### Verification

- `test_bash_command_approval.py`: conservative classification, one-off
  broker per command, strict no-broker refusal and isolation-only switch.
- `test_message_attachments.py`: magic-sniff, temporary vision/text blocks,
  no persisted base64, cross-session references, malformed paths.
- `test_file_commit_anchored.py`: root pinning and symlinked parent swap,
  mode preservation and exclusive create.
- `tests/native/windows_job_smoke.py`: real Windows process tree ownership.
- Repository full Python, API, Web E2E, Docker and MCP smoke.

### Open release gates

Command approval is not a shell AST parser and cannot prove a command safe.
Sandbox enforcement and tenant-scoped resources are the actual boundary.
Windows Job Object does not sandbox privileges. File commits still lack
non-cooperating-writer CAS. Uploaded files should eventually have durable
content-addressed metadata, scan/quarantine policy, per-tenant quotas,
automatic GC, provider-feature discovery and optional document conversion.

Do not merge until these security tradeoffs are explicitly accepted.
