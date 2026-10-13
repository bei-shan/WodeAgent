# Session tree branch semantics

Before exposing branch UI controls, the Python runtime must use branch-aware
model context. CodeAgent's ContextRuntime now builds prompts from the selected
ancestor chain, not the union of every sibling path.

On session activation, v2 snapshots also restore the explicit `cursor_id`
and `labels`; previously `load_entries` implicitly set the cursor to the
last serialized entry, causing a restored fork to point at the wrong branch.

Regression coverage includes: model prompt excludes abandoned messages,
snapshot labels/cursor survive restart, persisted AgentSession activation,
and malformed cyclic parent links cannot hang ancestor traversal.

Old v1 linear snapshots still restore through load_messages.
