"""PlanModeFeature — read-only analysis mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class PlanModeFeature(AgentFeature):
    """Switches the agent into a read-only planning mode.

    Only a subset of tools are available.  The plan text is injected
    into the next system prompt on exit.
    """

    name = "plan_mode"
    order = 60

    PLAN_MODE_TOOLS = {
        "Read", "Grep", "Glob", "LS", "TodoWrite",
        "TaskOutput", "EnterPlanMode", "ExitPlanMode", "AskUser",
    }

    def init(self, agent: "CodeAgent") -> None:
        agent._in_plan_mode = False
        agent._plan_text: Optional[str] = None
        # Move PLAN_MODE_TOOLS to agent for tool filtering.
        agent.PLAN_MODE_TOOLS = self.PLAN_MODE_TOOLS

    def pre_tool_use(
        self, agent: "CodeAgent", tool_name: str, tool_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Enforce plan-mode restrictions at execution, not just model discovery.

        A model or plugin can still supply calls that were not advertised in the
        current schema. This guard runs immediately before dispatch, including
        when a mode switch happens between tool calls in one model response.
        """
        if not getattr(agent, "_in_plan_mode", False):
            return None
        if str(tool_name or "") in agent.PLAN_MODE_TOOLS:
            return None
        return {
            "blocked": True,
            "reason": f"Tool '{tool_name}' is not allowed in plan mode.",
            "error_code": "PERMISSION_DENIED",
        }

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        blocks: list[str] = []
        if agent._plan_text:
            blocks.append(f"[Plan]\n{agent._plan_text}")
            agent._plan_text = None  # inject once
        return blocks
