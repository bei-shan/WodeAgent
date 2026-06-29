"""WorktreeFeature — git worktree session isolation."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent

logger = logging.getLogger(__name__)


class WorktreeFeature(AgentFeature):
    """Manages git worktree lifecycle for session-level file isolation."""

    name = "worktree"
    order = 20

    # The two tools this feature enables — gated by git availability.
    _WORKTREE_TOOLS = ("EnterWorktree", "ExitWorktree")

    def init(self, agent: "CodeAgent") -> None:
        from core.worktree.manager import WorktreeError, WorktreeManager

        worktree_store_dir = getattr(agent.config, "worktree_store_dir", "") or os.getenv("WORKTREE_STORE_DIR", ".worktrees")
        worktree_base_ref = getattr(agent.config, "worktree_base_ref", "") or os.getenv("WORKTREE_BASE_REF", "fresh")
        agent._worktree_manager = WorktreeManager(
            project_root=agent._original_project_root,
            store_dir=worktree_store_dir,
            base_ref=worktree_base_ref,
        )
        agent._active_worktree: Optional[dict] = None

        # Pre-check git availability so worktree tools can be cleanly hidden on
        # non-git project roots, instead of every tool call raising INTERNAL_ERROR.
        # The actual tool removal happens in post_init(), after _init_tools()
        # has registered them.
        agent._worktree_disabled = False
        try:
            agent._worktree_manager._ensure_git_available()
        except WorktreeError as exc:
            agent._worktree_disabled = True
            agent._worktree_disabled_reason = exc.message
            logger.warning(
                "WorktreeFeature: git unavailable at %s — worktree tools will be hidden (%s)",
                agent._original_project_root, exc.message,
            )

    def post_init(self, agent: "CodeAgent") -> None:
        # Tools are registered between init() and post_init(); remove now if disabled.
        if not getattr(agent, "_worktree_disabled", False):
            return
        for tool_name in self._WORKTREE_TOOLS:
            if agent.tool_registry.get_tool(tool_name) is not None:
                agent.tool_registry.unregister(tool_name)

    def cleanup(self, agent: "CodeAgent") -> None:
        # Worktree cleanup is handled by ExitWorktree tool.
        pass
