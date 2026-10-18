# Session isolation and legacy cleanup Implementation Plan

> Execute inline using executing-plans. Keep the user's 19 pre-existing edits intact; push reviewed source changes to a repair branch without deploying a public Agent endpoint.

**Goal:** Eliminate cross-session tool state sharing and retire only the old WodeAgent installation with a verified rollback archive.

**Architecture:** Each Agent owns its mutable registry. The Web tool endpoint projects metadata from live session registries without instantiating sessions. Cleanup targets named, stopped WodeAgent resources after validating ownership and backups; CityAIHub is preserved.

**Tech Stack:** Python 3.12, FastAPI, pytest, Docker/Compose, SSH.

## Constraints

- Work in the managed worktree copied from the current working files.
- Use offline scripted models, temporary directories and no paid provider.
- Do not share tool executors across sessions or weaken local/hosted boundaries.
- Never run global Docker prune, remove CityAIHub or expose API keys in output.
- Verify backup integrity before deleting any old server data.

## Task 1: Reproduce and fix default Web registry ownership

Files: `tests/test_web_session_tool_isolation.py`, `tests/test_web_service.py`, `desktop/service/app.py`.

- [x] Add real default-factory two-session tests for registry ownership, reads/writes, permissions, AskUser, deferred discovery, closure and read-only tool metadata listing.
- [x] Run `python -m pytest tests/test_web_session_tool_isolation.py -q`; verify it fails on shared ownership or wrong file content.
- [x] Construct `ToolRegistry()` inside each default Agent factory invocation, including the module entrypoint.
- [x] Make `/api/tools` aggregate and deduplicate tool metadata from live agents and any explicitly injected listing registry; keep uninitialized sessions lazy.
- [x] Replace the obsolete test requiring one shared mutable registry with metadata availability and independent-ownership assertions.
- [x] Run focused Web, MCP lifecycle, config, controller and tool tests, then the existing audit probe and offline eval.

### Review follow-up: MCP lifecycle

- [x] Reproduce failed-startup connection leaks, teardown/re-registration races and late background publication.
- [x] Close failed startup clients outside the lock and omit them from returned ownership lists.
- [x] Fence publication with registration generations; retire pending records and owned tools atomically with the last release.
- [x] Isolate later server configuration/constructor failures so prior connections retain correct ownership.
- [x] Validate deterministic blocked-connect/listing, zero-timeout, replacement-generation and lock-free-close cases with live local MCP transport regressions.

## Task 2: Retire legacy WodeAgent server installation

Inputs: exact stopped container IDs, bind paths `/home/ubuntu/projects/WodeAgent` and `/data/wodeagent`, volume `deploy_frontend-dist`, network `deploy_wodeagent-net`, image `deploy-api:latest`.

- [x] Capture CityAIHub IDs/start times and HTTP status as preservation baseline.
- [x] Verify old resource ownership, absence of outside consumers and sufficient backup disk space.
- [x] Create a mode-0700 backup directory; archive source/data/frontend volume, image data, API writable filesystem and private Docker metadata with mode-0600 files.
- [x] Verify gzip/tar integrity and SHA-256 manifests; retain a rollback guide.
- [x] Recheck exact stopped container IDs before removal, then remove only owned unused resources and validated source/data directories.
- [x] Confirm CityAIHub IDs/start times and HTTP health unchanged; record remaining resources and backup size.

## Task 3: Integration and delivery

- [x] Review the patch against session lifetime and cancellation boundaries.
- [x] Copy only the tested repair files back after checking the original files still match the initial baseline.
- [x] Run targeted tests and `git diff --check` on the user's checkout.
- [x] Record exact completed and outstanding production gates without claiming public readiness.

Windows validation excludes the previously failing secure-directory roundtrip test; Linux full regression and browser CI remain required on the published repair commit. Server load snapshots, rollback location and final GitHub/CI evidence belong in the local delivery record rather than committed credentials or private server metadata.
