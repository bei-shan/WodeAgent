"""DelegateModeFeature — restricted tool set for delegation mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class DelegateModeFeature(AgentFeature):
    """Restricts available tools to team-management only in delegate mode."""

    name = "delegate_mode"
    order = 40

    DELEGATION_ALLOWED_TOOLS = {
        "TeamCreate", "SendMessage", "TeamStatus", "TeamDelete", "TeamCleanup",
        "TeamApprovals", "TeamApprovePlan", "TeamFanout", "TeamCollect",
        "TeamTaskCreate", "TeamTaskGet", "TeamTaskUpdate", "TeamTaskList",
        "TodoWrite", "AskUser",
    }

    def init(self, agent: "CodeAgent") -> None:
        agent.delegate_mode = bool(
            getattr(agent.config, "delegate_mode", False)
        )
        agent.DELEGATION_ALLOWED_TOOLS = self.DELEGATION_ALLOWED_TOOLS
