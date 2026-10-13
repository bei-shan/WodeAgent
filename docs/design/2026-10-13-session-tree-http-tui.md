# Session tree HTTP/TUI capability

Routes:
- GET /api/sessions/{sid}/tree: nodes, children, labels and selected cursor.
- POST /api/sessions/{sid}/fork {target_id}: fork at an 8-char message ID.
- GET /history projects selected active branch from persisted v2 entries.

Before a fork the session must be idle. AgentSession protects the fork and
snapshot save under the same lock used to admit new turns. If persistence
fails, it restores the previous in-memory cursor and entries. All earlier
branches remain addressable; the operation moves a cursor rather than
deleting any messages.

TS OpenTUI /tree lists IDs/previews; /fork ID executes the API and reloads
selected branch history, rejecting invalid IDs locally. Neither command
forwards a slash command into the LLM.

Existing v1 snapshots continue to load as linear sessions. A tree request
initializes its lazy agent to obtain stable message IDs if necessary.
Busy sessions receive HTTP 409 for both tree and fork.
