exit_worktree_prompt = """## ExitWorktree

Exit the current worktree and restore the agent's working directory
to the original project root.

Parameters:
- action: (required) "keep" | "remove"
  - "keep":   Preserve the worktree and branch.  The user can later
              review with ``git diff wt/{name}`` and merge with
              ``git merge wt/{name}``.
  - "remove": Delete the worktree and its branch.  If there are
              uncommitted changes you must also pass
              discard_changes=true.
- discard_changes: (optional, default false) Only valid when
  action="remove".  When true, discard all uncommitted changes
  in the worktree.

Behaviour:
- If the worktree is clean (no changes), it is removed automatically
  regardless of the *action* parameter — no need to explicitly choose
  "remove".
- If the worktree has changes and action="keep", the worktree and
  branch are preserved for later review.
- If the worktree has changes and action="remove" with
  discard_changes=true, the worktree and its branch are deleted.
- If the worktree has changes and action="remove" with
  discard_changes=false, the call is rejected — you must choose to
  keep or discard.
"""
