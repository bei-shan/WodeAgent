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

### 5. Attachment pipeline (images and documents)

```text
ChatComposer uploads file bytes
  -> POST /sessions/<id>/upload
     streamed 25 MiB cap; O_NOFOLLOW; exclusive create
     -> opaque random attachment_ref scoped to session + file identity (2 h)
  -> POST /sessions/<id>/messages
     { content, attachments: ["opaque-attachment-ref"] }
  -> WorkspaceService.prepare_attachments()
     resolves ONLY issued upload credentials; refuses guessed paths,
     other sessions, changed files and expired grants
  -> bounded magic-sniffing and parsing (max 4 files, 8 MiB combined)
     * PNG/JPEG/GIF/WebP: Pillow verify, <= 4 MiB, <= 16 megapixels
       -> OpenAI-compatible image_url content block
     * UTF-8 text/code: <= 96 KiB payload, 40K-character text part
     * PDF: <= 3 MiB, <= 12 pages, pypdf bounded text derivative
     * DOCX: <= 3 MiB, <= 2 MiB inflated word/document.xml text
     * other binary: path-only reference, never fake text/vision
  -> AgentLoop attaches multimodal parts to the newest user turn on
     EVERY model step during that turn (including after tool calls)
  -> session history/trace: text and file reference names, NO base64
  -> discard ephemeral model parts on completion, exception or cancellation
```

Attachments supplied via the API are **upload credentials**, not
filesystem paths. Merely placing a file in the workspace does not grant a
model attachment capability. Credentials are random, in-memory,
session-scoped, limited to 128 per session and expire after two hours. The
actual file is verified by inode/device, size, nanosecond mtime and ctime
at message admission; uploads modified by a tool must be uploaded again.
The implementation is intended for the **single-process local Web service**.
Multi-worker/hosted deployments need a durable per-tenant attachment store,
authorization-aware object keys and signed temporary fetch URLs; this
in-memory ticket registry is not appropriate there.

Document text is explicitly labelled as **untrusted attachment data**, not
instructions. Raw PDF/DOCX bytes are never sent as a fabricated image_url
and cannot be interpreted merely by suffix. Scanned/image-only PDFs yield
no text and require a separately authorized OCR/document conversion service.
Unsupported binary files are not claimed as understood by the model.

**Parsing security limitation:** Pillow, pypdf and ZIP/XML parsing still run
in a local process on the API worker. Byte/page/pixel/XML caps reduce exposure
but do NOT provide CPU or memory isolation for malicious documents. Hosted
production must execute conversion in an isolated, resource-limited worker
and scan/quarantine uploads before model use.

Binary originals persist in the session's workspace even though media
base64 is not put into chat history. Files need explicit retention/GC and
storage-at-rest policies before hosted use. A model/provider without vision
support may reject an image request; fallback must be surfaced rather than
claiming successful interpretation.

### Verification

- `test_bash_command_approval.py`: conservative classification, one-off
  broker per command, strict no-broker refusal and isolation-only switch.
- `test_message_attachments.py`: real image decoding, transient vision/text
  parts, no persisted base64, upload tickets, cross-session denials, modified
  upload refusal and malformed path handling.
- `test_attachment_parsers_and_agent_loop.py`: DOCX text, ZIP bombs, encrypted
  PDF rejection, forged image rejection and image context across tool steps.
- `test_file_anchored_reads.py`: pinned file and parent directories with
  symlink attacks, edit size bounds and POSIX native feature detection.
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
