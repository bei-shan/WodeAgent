# Portable conversation tree import/export (2026-10-13)

Format: ridgecode.portable-session version 1. Deliberately **not** a raw
internal snapshot or a full runtime restore; secrets and transient state
must never cross the boundary.

Public HTTP:
- GET /api/sessions/{sid}/export — no-store portable JSON; busy turns 409
- POST /api/sessions/import — stream-bounded application/json, new session ID

Portable allowlist: title, cursor, visible user/assistant messages with
eight-char IDs and explicit parent, and labels. All other fields (system
prompts, tools, tool calls/output, metadata, read caches, Teams state, model
config, worktrees, private files, credentials) are deliberately discarded.

Strict schema, chronological tree edges, supported roles and unique IDs;
maximum 500 messages, 4096 chars per message, 1 MiB JSON and 120-char
title. Imports never overwrite an existing session, and validation occurs
before any persistent catalog writes. Archive parse rejects duplicate keys.

Terminal commands /export FILE and /import FILE operate on the terminal
machine, never the remote server filesystem. Exports create new 0600 files
exclusively; imports have bounded reads and refuse final-path symlinks.

Users should treat the resulting JSON as potentially private chat content.
Exporting it is not an Agent backup: after import, tools and Teams state
must be recreated separately and only visible messages are retained.
