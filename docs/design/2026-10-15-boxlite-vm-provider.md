# Experimental BoxLite micro-VM provider (2026-10-15)

Ridge Code's local coding Agent now has three explicitly selected execution modes:

| `AGENT_SANDBOX_MODE` | Bash execution | Trust boundary |
| --- | --- | --- |
| `local_trusted` (default) | Host process, legacy tools available | Trusted single user; **not** isolation |
| `docker` | Hardened Docker command, disposable container | Container, network disabled, workspace RW; signed daemon cleanup |
| `boxlite` | BoxLite hardware micro-VM, disposable instance | Guest kernel, network disabled, workspace RW; optional, experimental |

## Reference and scope

The design follows [DeerFlow's BoxLite provider](https://github.com/bytedance/deer-flow/tree/main/backend/packages/harness/deerflow/community/boxlite)
and the [official BoxLite SDK](https://github.com/boxlite-ai/boxlite/blob/main/docs/reference/python/README.md).
DeerFlow uses a per-thread warm VM pool, structured `SandboxProvider` lifecycle,
virtual `/mnt/user-data` paths, and separate implementations for local/Docker,
BoxLite and E2B. Ridge Code currently exposes *only* a command provider
interface (`core/sandbox/contracts.py`); it does **not** implement DeerFlow's
full multi-operation sandbox protocol or its warm-pool lifecycle.

The `BoxliteCommandProvider` runs each command in a fresh `SimpleBox` context
with `auto_remove=True`, disabled networking, CPU/memory caps and
`volumes=[(workspace, "/workspace", False)]` (host bind **read-write**).
Only this explicit volume persists between calls. This is a local-development
tradeoff: a compromised guest or deliberately destructive command may alter
any writable workspace content. Use a **dedicated disposable workspace**, not
a home directory or a repository holding production secrets. The provider
refuses to include the local Docker owner signing key in a mounted workspace.

No model arguments can set image, volume paths, guest network, or resource
caps. `Read`, `Write`, `Edit`, Skills, MCP, AgentTeams, functions, subagent
tools, and Worktree switching remain unavailable from the model in isolated
mode; the ToolRegistry restriction is applied before the first model step.
BoxLite unavailability or guest failure returns an error: there is no
host Bash or Docker fallback.

## Setup

1. Use Linux with working KVM (nested virtualization on a VM host), or a
   supported macOS machine with Hypervisor.framework. This mode is not offered
   on Windows.
2. Install a compatible version of the optional `boxlite` Python SDK in the
   Agent's environment, following its official platform wheel guidance.
3. Choose an approved OCI image and opt in:

   ```dotenv
   AGENT_SANDBOX_MODE=boxlite
   AGENT_SANDBOX_BOXLITE_IMAGE=python:3.12-slim
   AGENT_SANDBOX_BOXLITE_MEMORY_MIB=1024
   AGENT_SANDBOX_BOXLITE_CPUS=2
   ```

4. Start the Agent in a disposable project directory without credentials.
   The VM image and SDK/host capabilities must be provisioned by the operator.

## Cancellation and limitations

The provider passes a per-command SDK deadline and independently polls
the Agent's turn-scoped cancellation token. On Stop/timeout it cancels the
asynchronous guest-exec task and exits `SimpleBox`'s async context; this is
best effort if the underlying BoxLite runtime hangs. No claim of enforced
process-tree termination, VM cleanup after a host crash, or multi-tenant
isolation is made until separately tested against live KVM/HVF.

Unlike DeerFlow's warm BoxLite pool, this initial backend pays VM startup cost
for *every* Bash call. It has no multi-session VM ownership leases, no structured
guest file API, no uploaded skills projection, no artifact syncing beyond the
RW bind mount, and no E2B/Kubernetes provider. Docker remains the better-tested
isolation path for current v0.1 candidates.

## Verification

```bash
python -m pytest tests/test_sandbox_boxlite.py tests/test_sandbox_boundary.py -q
AGENT_BOXLITE_INTEGRATION=1 python -m pytest tests/test_sandbox_boxlite_integration.py -q
```

CI uses a fake BoxLite SDK to exercise backend selection, path traversal,
network configuration, cancellation, timeout, guest failure and host tool
denial. A live BoxLite KVM/HVF integration matrix remains an explicit release
gate and is **not** claimed by the mocked tests.

## Next architecture stages

- Formal session-scoped acquire/release, per-user ownership and warm VM pool
  with explicit max capacity, idle TTL, and interrupted creation cleanup.
- Structured `execute_command/read_file/write_file/list_dir` provider contracts
  with portable guest file exchange and write policy; split temporary results
  from durable workspace state.
- E2B cloud micro-VM adapter with remote leases, Redis coordination for
  multi-gateway installs, mount uploads and bounded artifact download.
- Independent VM and Docker security acceptance, including real hardware
  virtualization, tool cancellation, symlink races, memory limits and recovery.

Full VM parity with DeerFlow should be evaluated by behavior and tests, not
by copying source files or exposing unsupported provider names as active.
