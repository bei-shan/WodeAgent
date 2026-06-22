"""WorktreeFeature — git worktree session isolation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Optional

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class WorktreeFeature(AgentFeature):
    """Manages git worktree lifecycle for session-level file isolation."""

    name = "worktree"
    order = 20

    def init(self, agent: "CodeAgent") -> None:
        from core.worktree.manager import WorktreeManager

        worktree_store_dir = os.getenv("WORKTREE_STORE_DIR", ".worktrees")
        worktree_base_ref = os.getenv("WORKTREE_BASE_REF", "fresh")
        agent._worktree_manager = WorktreeManager(
            project_root=agent._original_project_root,
            store_dir=worktree_store_dir,
            base_ref=worktree_base_ref,
        )
        agent._active_worktree: Optional[dict] = None

    def cleanup(self, agent: "CodeAgent") -> None:
        # Worktree cleanup is handled by ExitWorktree tool.
        pass
