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

### 5. Local Web attachment pipeline (images and documents)

```text
ChatComposer selects files
  -> POST /sessions/<id>/upload
     25 MiB upload stream -> O_NOFOLLOW / exclusive file in session root
     optional clamd Unix socket INSTREAM scan (mandatory deployment gate)
     -> SQLite SHA-256(upload_ticket) + file identity / tenant / session
     -> opaque ticket returned to the browser, 2-hour TTL
  -> POST /sessions/<id>/messages
     { content, attachments: ["opaque-upload-ticket"] }
  -> WorkspaceService.prepare_attachments()
     SQLite credential/session/tenant lookup -> descriptor-pinned file
     -> unchanged inode/device/size/nanosecond mtime/ctime verification
  -> bounded input processing (4 files, 8 MiB combined)
     * PNG/JPEG/GIF/WebP: verify <=4 MiB, <=16 megapixels -> image_url
     * UTF-8 text/code: <=96 KiB -> bounded untrusted text
     * PDF/DOCX: <=3 MiB -> Docker converter (production mode)
     * unknown binaries: path-only, never fabricated image/text
  -> AgentLoop includes ephemeral media in every step of this one turn
  -> persistent Agent history: filenames and user text only, never base64
  -> discard ephemeral model blocks at turn completion/failure
```

**Local authorization durability.** Upload grants are SHA-256 token hashes
in SQLite (`desktop/service/application/attachment_store.py`), bounded to
the local tenant marker, session, filename, device/inode, size and ns-level
mtime/ctime. SQLite `BEGIN IMMEDIATE` transactionally enforces a maximum
128 active files and 100 MiB per session, 1 GiB across the local store.
Other session/tenant lookups are rejected, and upload state can be recovered
after local API restart. Records expire after 2 hours. Startup and an hourly
task revoke expired grants and unlink unchanged files using anchored session
directory handles. If cleanup gets a transient OS failure, the record remains
for retry; user-replaced files are not unlinked.

**Local-only scope:** This is *not* a distributed object store. The hosted
metadata-only API intentionally exposes no Agent tools or upload routes.
Shared cloud storage, per-principal object ACLs, durable GC leases, encryption
at rest, tenant billing/quotas and worker ownership are still required for an
actual hosted multi-tenant attachment product. The SQLite `local` tenant is
not a substitute for OIDC/RLS authorization.

**Malware scanning.** The optional clamd Unix-socket INSTREAM integration
scans the already-open file descriptor before publishing an upload token.
`RIDGE_ATTACHMENT_SCAN_REQUIRED=1` rejects all uploads if
`RIDGE_CLAMD_UNIX_SOCKET` is not configured or clamd is unavailable,
times out, reports `FOUND` or returns a protocol error. Uploaded file
identity is rechecked after scanning. This is a configurable local antivirus
gate, not a complete quarantine solution, and real ClamAV virus-definition
management and service lifecycle need independent operational acceptance.
An untrusted Agent running simultaneously in the same local workspace may
see a not-yet-scanned filename; a hosted system must use separate pre-scan
quarantine storage.

**Document execution modes.**

- `RIDGE_DOCUMENT_CONVERTER_MODE=local` (default): deadline-limited
  separate Python process with Linux CPU/address-space limits. Allowed
  only for explicitly trusted local desktop use.
- `RIDGE_DOCUMENT_CONVERTER_MODE=docker`: prebuild with
  `docker build -f deploy/document-converter/Dockerfile -t ridge-document-converter:local .`.
  Docker runs with `--pull=never --network=none --read-only`,
  no host mounts or API secrets, `--cap-drop=ALL`,
  `no-new-privileges`, non-root UID, CPU, memory/PID/tmpfs
  caps and an 8-second parent deadline. The container is force-reaped
  when needed. There is **no local parser fallback** if Docker fails.
- `RIDGE_DOCUMENT_REQUIRE_SANDBOX=1` (or hosted deployment mode):
  fail closed if Docker mode is not selected. The image name can be
  set using `RIDGE_DOCUMENT_IMAGE`; the required image must already exist.

Typed document contents are marked as untrusted attachment data. A PDF
containing scanned images without a text layer yields no extracted text;
it is not equivalent to OCR. The Docker converter handles PDF/DOCX text
only; other documents remain references. Model/provider capabilities must
support image_url parts or reject them honestly.

### Production release boundary

The command approval classifier is a conservative gate, not a general
shell AST proof; host Bash must be disabled for untrusted deployments.
A Docker daemon socket is privileged: move this document conversion into a
dedicated worker/daemon or remote execution service for hosted deployment,
not a public-facing multi-tenant API with unrestricted Docker access.
Do not confuse Job Object process ownership with OS permission isolation.
File edits use directory locks and metadata CAS for cooperating writers,
not atomic compare-and-swap against arbitrary external programs. Native
Windows handle-scoped file safety still needs parity with POSIX.

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
- `test_attachment_store_durability.py`: hashed tickets across instances,
  tenant/session scopes, concurrent SQLite quota admission, restart and TTL
  unlink with user-replacement preservation.
- `test_attachment_scanner.py`: real fake-Unix-socket clamd INSTREAM frame
  exchange, clean and FOUND verdicts, missing scanner fail-closed behavior.
- `test_document_container_policy.py`: Docker argv hardening, no mounts or
  network, strict fail closed on timeout and missing Docker.
- `test_document_container_smoke.py` and the standalone
  `document-converter-isolation.yml` workflow: real image build and Docker
  PDF/DOCX + malformed input tests.
- Repository full Python, API, Web E2E, Docker and MCP smoke.

### Open release gates

The local SQLite grant database and hourly TTL sweeper do not implement
hosted multi-tenant object storage with real OIDC scope enforcement,
encryption-at-rest, multi-host GC leases, key rotation, storage quotas,
malware quarantine and deletion SLA. Fail-closed clamd support is not a
verified production ClamAV service. Real cloud deployment and hostile-tenant
stress/soak remain blockers; do not equate local regression success with them.

Do not merge until these security tradeoffs are explicitly accepted.
