# Session tree: branch-scoped, non-destructive context compaction

The previous compactor summarized the global `_messages` list, including
abandoned sibling branches, and discarded earlier nodes. Branch-specific
model prompts from PR #35 alone were insufficient.

HistoryManager now uses a separate compaction path when a fork exists:
- sources are selected-path messages, never global history
- an active summary is recorded in a branch-scoped compaction entry
- a summary overlay replaces earlier context only for descendants of the
  cursor where that compaction occurred
- all raw history nodes are retained for navigation and future forks
- restore from history_entries/cursor_id reapplies the selected overlay
- subsequent compactions incorporate only the previous active summary and
  later messages, without reintroducing abandoned siblings

Unforked, linear histories still use the existing compaction algorithm
to avoid changing legacy token-budget behavior in one migration.
