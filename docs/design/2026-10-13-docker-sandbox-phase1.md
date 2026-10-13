# Docker Sandbox Mode — Experimental Command Boundary

This is an **opt-in, Docker-only tool execution mode** for trusted local
developers. It follows the main separation in [DeerFlow's sandbox provider](https://github.com/bytedance/deer-flow/tree/main/backend/packages/harness/deerflow/sandbox):
host-local subprocesses are **not** a secure sandbox, and container execution
must be distinct from a workspace-path permission check.

## Modes

| Mode | Config | Behavior |
| --- | --- | --- |
| Existing local mode | `AGENT_SANDBOX_MODE=local_trusted` (default) | Backward compatible. Bash and file tools run on host. **Not a security boundary** |
| Restricted Docker | `AGENT_SANDBOX_MODE=docker` | Model can invoke **only** `Bash` through a restricted ephemeral container; host Read, Edit, Write, Grep, Glob, LS, skills/MCP, custom functions, subagents and worktree switching are denied or hidden via ToolRegistry |

> This first phase does **not** implement arbitrary multi-tool Docker file
> APIs. Instead of mixing isolated Bash with unisolated host file tools, the
> policy rejects those host tools completely. Commands may use `cat`, `grep`,
> `python`, etc. **inside the container**. Parent framework code, Web API file
> routes and explicitly imported Python plugins are *not* isolated by this tool
> gate. Do not treat this as whole-service sandboxing or authentication.

## Trusted setup

1. Install Docker Engine on a POSIX host, ensure the daemon is available to
   the trusted operator, and pre-pull the selected image **before** launching
   the Agent (`docker pull python:3.12-alpine`). Docker API/daemon control is
   highly privileged and must not be given to agents.
2. Use a **dedicated, disposable workspace directory** containing no personal
   tokens, `.env`, `.ssh`, credentials or confidential host files. In Docker
   mode the workspace is mounted read-write at `/workspace`; anything inside
   is visible to the executed command and may be overwritten.
3. Enable in `.env`:

   ```dotenv
   AGENT_SANDBOX_MODE=docker
   AGENT_SANDBOX_DOCKER_IMAGE=python:3.12-alpine
   AGENT_SANDBOX_MEMORY=512m
   AGENT_SANDBOX_CPUS=1.0
   AGENT_SANDBOX_PIDS_LIMIT=64
   ```

4. Run with `project_root` pointed at that workspace. Docker must already
   have the image: the executor sets `--pull=never` and never silently runs
   failed Docker commands on the host. The Web app remains **local-only** and
   lacks authentication.

The container CLI uses `docker run --rm` per command, `--network none`,
read-only root filesystem, non-root `--user`, drops all Linux capabilities,
sets `no-new-privileges`, bounds pids/memory/CPU, and provides an ephemeral
`/tmp`. Only `/workspace` is bound from the host. The command is an argv
element to `/bin/sh -lc` inside Docker, not interpolated into a host shell.
Daemon-managed container names are random; on client timeout the executor
requests `docker rm --force`. Docker daemon failures do not enable fallback.

All container commands are **fresh** processes: shell aliases, `cd`, env
changes and temporary `/tmp` files do not persist between calls; file
modifications under `/workspace` do persist.

## Known remaining gaps

- **No multi-user or Web authentication.** Existing web endpoints, WebSocket,
  files, workspace upload and tools are not owner-scoped. Do not expose the
  Web API to the Internet, a LAN, or untrusted browser origins.
- **Not a full OS confinement audit.** The Docker daemon, kernel, preloaded
  image, host filesystem mount and configuration are trusted. The workspace
  volume might contain symlinks, secrets, git credentials, hooks and user
  files; those are not auto-filtered. A non-root UID can still change any file
  it has permission to write.
- **No containerized MCP/Skills/agent teams yet.** They are denied in Docker
  mode rather than allowed through unisolated host code. It is possible for
  Python framework plugins that execute before the tool dispatcher to access
  the host; do not import untrusted plugin Python.
- **Docker timeout cleanup is best effort.** Unexpected host crashes can
  leave ephemeral containers behind; recovery/garbage collection and managed
  per-session leases are planned.
- Container filesystem APIs, read-only source overlays, upload/download file
  exchange, user-scoped isolation, NetworkPolicy, seccomp/AppArmor profiles
  and container runtime hardening are next-stage work.
- **Tests with a fake Docker subprocess validate CLI arguments and tool
  policy, not actual container isolation.** A separate test on a machine
  with Docker and a preloaded image is needed for containment acceptance.
- The Image name is configured by the trusted operator, not the model.
  Prefer pre-verified image digests for reproducibility and stronger
  supply-chain assurance.

## Acceptance test boundaries

```bash
python -m pytest tests/test_sandbox_boundary.py tests/test_bash_tool.py -q
```

For host integration, manually verify a Docker-enabled Agent can run a
workspace-only test command; attempts to read another host user's directory,
use `--network none` to reach the Internet, or invoke model-supplied
`Read`/`Write`/MCP tools must fail. Check timeout container cleanup with
`docker ps -a`. For actual multi-user isolation, first build authenticated
owner binding, CSRF and WS-origin checks; path-prefix tests alone are not
sufficient.
