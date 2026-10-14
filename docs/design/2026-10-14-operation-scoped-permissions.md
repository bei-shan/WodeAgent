# Operation-scoped path authorization — 2026-10-14

## Problem and trust boundary

`PermissionGate` previously cached interactive decisions by canonical path
only. A user approving `Read` of an out-of-root file could then have that
approval reused by `Write`, `Edit` or `MultiEdit`. Canonical paths and
symlink checks prevent alias confusion, but path identity alone does **not**
encode the operation the user authorized.

This patch adopts the ordered, capability-aware permission principle from
[OpenCode](https://github.com/anomalyco/opencode), without changing the existing
Web prompt transport. Permission decisions remain local, per-session and
in-memory.

## Policy

- `Read` requests the `read` capability for a canonical target.
- `Write`, `Edit`, `MultiEdit` share a `write` capability. Read permission
  never authorizes mutation, and write permission does not silently grant read.
- Unknown action labels are isolated in their own scopes; they cannot borrow
  read/write consent.
- Approved/denied prompt decisions are cached as `(canonical_path, capability)`.
  A non-interactive subagent shares decisions but cannot escalate permissions.
- The legacy explicit `PermissionGate.grant(path)` API continues to create a
  wildcard for already-trusted framework callers. It is **not** used for user
  prompt approvals; future capability API migration should make wildcard grants
  an explicitly privileged operation.
- Permanent sensitive-path denials, hard sandbox mode, and out-of-root
  noninteractive default-deny behavior remain unchanged.

## Verification

```bash
python -m pytest tests/test_permission_gate.py tests/test_permission_canonical_boundaries.py tests/test_permission_action_scopes.py tests/test_read_tool.py tests/test_write_tool.py -q
```

The new integration test exercises a real Read/Write tool pair against a
disposable out-of-root file: approving Read never permits a subsequent write,
and the file content remains unchanged.

## Remaining limits

This is an authorization cache fix, **not** a filesystem confinement boundary.
Symlink swaps between check and use, direct host shell execution in trusted
local mode, executable Python plugins and externally granted OS permissions
still require independent sandbox/TOCTOU controls. Keep Docker mode for
untrusted shell workloads and do not expose local mode as multi-tenant SaaS.
