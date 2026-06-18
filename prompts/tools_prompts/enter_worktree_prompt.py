enter_worktree_prompt = """## EnterWorktree

Create or enter a git worktree, switching the agent's working directory
into an isolated workspace.  All subsequent file operations (Read, Write,
Edit, Bash, Glob, Grep, etc.) will operate inside the worktree.

Parameters:
- name: (optional) Name for a new worktree.  A branch ``wt/{name}`` is
  created automatically and the worktree lives at ``.worktrees/{name}/``.
- path: (optional) Absolute path of an existing worktree to re-enter.

  Provide exactly one of *name* or *path*.

Behaviour:
- When *name* is given a new worktree is created.
- When *path* is given the existing worktree is re-entered.
- Sub-agents (Task, AgentTeams) automatically inherit the worktree.

Use this when:
- You need to make experimental / risky changes without polluting the
  main working tree.
- You want to try a refactor that the user can review before merging.

After finishing work inside the worktree, call ExitWorktree to return to
the original project root.  Use ExitWorktree(action="keep") to preserve
the worktree for later review/merge.
"""
