# P0 Sandbox workspace snapshots and private artifacts — acceptance

This change introduces two opt-in, isolated primitives:

1. `SandboxWorkspaceSnapshot` creates a bounded disposable copy of a trusted
   project before launching a Docker guest. It rejects symlinks, special files,
   credential names, oversized source trees and files; excludes `.git`, caches,
   local virtualenvs and dependency directories. Guest writes never touch the
   live host tree. The service owner closes the copy on Agent shutdown.
2. `ScopedArtifactStore` stores bytes in a private, no-follow local blob
   directory and indexes owner metadata in SQLite. Each read/delete is scoped
   to `(tenant,user,project,agent,session)`, with TTL, MIME and resource limits.
   Artifacts are NOT mounted or published automatically. Authorization still
   MUST come from a verified server-side identity and session owner.

Enable the snapshot **only in trusted non-root POSIX Docker deployments** with
`AGENT_SANDBOX_WORKSPACE_SNAPSHOT=true`. It is currently **opt-in** while
the writeback/download protocol and deployment compatibility are being
finalized. No implicit copy-back exists. An operator-owned adapter can call
`export_guest_file(path, authorize=trusted_exact_file_approval)` to receive
bounded bytes only after source conflict verification. A separate verified,
race-resistant file write API must still perform the actual write; this API
never automatically modifies the host workspace.

## Evidence gates

- `tests/test_scoped_artifacts.py`: distinct tenant/session, TTL, quota,
  path traversal, symlink swap, deletion and persistent local metadata.
- `tests/test_workspace_snapshot.py`: transient snapshot, confidential
  file/symlink checks, source conflict, explicit export and Docker argv mount.
- `tests/test_sandbox_snapshot_docker_integration.py`: **real Docker daemon**
  and guest shell; verifies guest writes land ONLY in the snapshot, source
  remains unchanged, exact export approval and snapshot cleanup.
- Always-on `real-integration-gates.yml` invokes Docker integration tests
  with a fail-closed JUnit gate: any skip fails the job.

## Not yet production-complete

- POSIX file staging is not an atomic filesystem snapshot: attacker-controlled
  files may be swapped during copying and direct source writeback retains a
  race unless implemented with stable directory handles and conflict-checked
  atomic operations. Do not run this mode on adversarial shared directories.
- Export is deliberately not wired to an HTTP download or model tool until
  authenticated API authorization, audit, content scanning and resource
  expiration are integrated.
- SQLite local artifact blobs are single-node development storage, not a
  horizontally scalable multi-tenant object store. No durable distributed
  transaction coordinates SQL metadata with filesystem blobs.
- BoxLite and E2B require separate guest-staging and controlled artifact
  export paths; Docker acceptance cannot stand in for VM/cloud evidence.
- Snapshot flag defaults to disabled; this is an incremental hardening
  delivery, NOT a complete removal of legacy live RW workspace mounts.
