# Workspace/File Service extraction — 2026-10-13

## Boundary

HTTP routes own FastAPI validation, authentication and streaming responses.
WorkspaceService owns session-root resolution, directory listing, previews,
download handles and streamed uploads.

The dependency direction is Web / TUI → FastAPI routes → WorkspaceService
→ SessionController and filesystem.

The service does not import FastAPI or frontend modules. It is initialized per
FastAPI app with the same controller and data directory as SessionService.

## Security and compatibility

- Only active sessions can read or upload files; missing IDs return HTTP 404.
- All resolved read/list/download targets must stay within the session root.
  Traversal and symlink escapes return 403.
- Directory listings suppress symlinks; UTF-8 previews over 2 MiB are rejected.
- Uploads use basename filenames and O_EXCL, are streamed in 64 KiB chunks,
  and never overwrite an existing file.
- Uploads over 25 MiB return HTTP 413 and delete partial files.
- Download streams use open handles with O_NOFOLLOW where provided; aborted
  transfers release handles.
- Existing route shapes and success response formats remain compatible.

## Deferred security work

Use descriptor-relative POSIX traversal to further reduce symlink replacement
races in intermediate path components. A hostile process with write access to
a workspace should not be treated as a trusted client.

Introduce WorkspaceProvider / ArtifactStore interfaces so Docker or remote
sandbox files need not expose local filesystem paths. Continue isolating
process-global Config and extracting ToolRuntime / AgentLoop ports.
