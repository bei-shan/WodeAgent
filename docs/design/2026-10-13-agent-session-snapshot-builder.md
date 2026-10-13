# Agent session snapshot assembly

The v2 snapshot envelope is now projected by
core.runtime.session_snapshot.SessionSnapshotBuilder via a typed SnapshotHost
port. CodeAgent._build_snapshot remains a compatibility method; both
auto-save and legacy save_session(path) use the exact same snapshot
projection instead of duplicating the schema construction.

This eliminates drift between saved UI sessions and CLI-exported snapshots:
system/history, context metadata, read cache, active worktree, team state,
tree entries, labels, cursor and model selection are captured once.

The assembler does not own the SessionStore or run loops. SessionManager
continues to own durable catalog records, AgentSession owns active turn
threads, and TeamCheckpointService owns its own embedded team state.
The next extraction should move full save/resume/list session commands to
a SessionRepository port, with backward-compatibility tests for old v1
snapshots; do not change session identifiers in the engine refactor.
