"""Worktree module — git worktree session isolation.

Provides EnterWorktree / ExitWorktree tools aligned with Claude Code's
session-level worktree switching model.

Exports
-------
WorktreeManager : manages git worktree lifecycle
WorktreeStore   : persists worktree index to disk
WorktreeError   : typed error with code/message for tool mapping
"""

from .manager import WorktreeManager, WorktreeError
from .store import WorktreeStore

__all__ = ["WorktreeManager", "WorktreeStore", "WorktreeError"]
