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

    def pre_tool_use(
        self, agent: "CodeAgent", tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Block non-whitelisted tools in delegate mode.

        Previously hardcoded in ``CodeAgent._execute_tool`` (P1 #11 fix).
        """
        if not agent.delegate_mode:
            return None
        if str(tool_name or "") in agent.DELEGATION_ALLOWED_TOOLS:
            return None
        return {
            "blocked": True,
            "reason": f"Tool '{tool_name}' is not allowed in delegate mode.",
            "error_code": "PERMISSION_DENIED",
        }
