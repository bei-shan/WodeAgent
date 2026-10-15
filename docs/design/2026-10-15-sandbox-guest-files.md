# Guest-side structured file protocol (2026-10-15)

This is the next isolated-execution slice of [Issue #75](https://github.com/bei-shan/WodeAgent/issues/75).
`core/sandbox/guest_files.py` provides `GuestFilesystem` and a
`SandboxFilesystem` protocol with bounded `read_file`, `write_file`, and
`list_dir` methods.

## Trust boundary

All target-file system calls are executed **inside the selected Docker or
BoxLite guest** through the `SandboxCommandProvider`. The host launcher
builds an argv-safe shell command for a constant Python program. It never
opens, reads, stats, writes or deletes model-targeted host files. The caller
supplies a path under the guest's `/workspace` root; parent traversal,
control characters, outside absolute paths and oversized names are refused
before dispatch.

The guest program opens `/workspace` and walks each directory via
`open(..., dir_fd=..., O_DIRECTORY|O_NOFOLLOW)`. Read uses
`O_NOFOLLOW|O_NONBLOCK` on the final file and requires a regular file.
Writes use a new mode-0600 temporary file under the verified directory
descriptor and `os.replace(..., src_dir_fd=..., dst_dir_fd=...)` to avoid
following a swapped target symlink. Existing symlinks and special files are
rejected. This prevents the common check-then-use symlink pivot **inside the
guest**; it does not prove the host bind mount or Docker/BoxLite runtime safe
from a privileged daemon/operator.

## Limits

- A single binary read or write is capped at **64 KiB**.
- A directory listing is capped at **128 entries**, failing rather than
  silently producing an incomplete result.
- Result payloads are capped at 160 KiB and validated before decoding.
- All data is binary-safe using base64 over a bounded JSON guest response.
- Guest command timeout/interrupt is forwarded through the existing Docker
  cancellation or BoxLite VM path. Failures never run local filesystem tools.
- Both supported default Python images provide `python3` in the guest.
  Other operator-chosen images must include Python 3 to use this API.

This is an **internal provider API**. The model still sees only isolated Bash
in Docker/BoxLite mode; legacy host Read/Write/Edit/MCP tools remain sealed.
Do not register host file tool wrappers against this API without separate
capability/permission, auditing and write-conflict tests.

## Verification

```bash
python -m pytest tests/test_sandbox_guest_files.py -q
AGENT_DOCKER_INTEGRATION=1 python -m pytest tests/test_sandbox_docker_integration.py -q
```

CI covers mocked protocol failures and a **real Docker** symlink and binary
round trip. Real BoxLite KVM/HVF acceptance, file upload/download larger than
the single-command budget, safe artifact publication, and concurrent VM
workspace mutation audits are remaining Issue #75 tasks.

## Non-goals

No remote E2B support, warm-pool cross-process ownership, filesystem snapshot
API, tools auto-exposed to models or multi-tenant authority are added here.
No network permission or host execution fallback is introduced.
