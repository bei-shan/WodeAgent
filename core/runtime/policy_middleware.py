"""Core policy middleware implementations; not plugins or resource owners.

These policies are created by the framework before user-contributed feature
hooks. A plugin cannot override their names or short-circuit their denial.
Legacy Feature classes are compatibility adapters.
"""
from __future__ import annotations

from typing import Any, Optional

class DelegateModeMiddleware:
    """Restricts available tools to team-management only in delegate mode."""

    name = "delegate_mode"
    order = 40

    DELEGATION_ALLOWED_TOOLS = {
        "TeamCreate", "SendMessage", "TeamStatus", "TeamDelete", "TeamCleanup",
        "TeamApprovals", "TeamApprovePlan", "TeamFanout", "TeamCollect",
        "TeamTaskCreate", "TeamTaskGet", "TeamTaskUpdate", "TeamTaskList",
        "TodoWrite", "AskUser", "ToolSearch",
    }

    def start(self, agent: "CodeAgent") -> None:
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


class BudgetMiddleware:
    """Tracks token consumption against an optional budget.

    Budget is parsed from user input (e.g. ``+500k``) and consumed
    each LLM call.  Status is injected into runtime blocks.

    When ``config.budget_enforce`` is True, this feature additionally
    blocks tool execution once ``is_exceeded()`` returns True. Default
    is ``False`` — tracking is purely informational unless the user
    opts in.
    """

    name = "budget"
    order = 55

    def start(self, agent: "CodeAgent") -> None:
        from core.budget_tracker import BudgetTracker

        agent._budget_tracker = BudgetTracker()

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        status = agent._budget_tracker.status_text()
        return [status] if status else []

    def pre_tool_use(
        self,
        agent: "CodeAgent",
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Block tool execution when the budget is exhausted (opt-in)."""
        if not getattr(agent.config, "budget_enforce", False):
            return None
        tracker = getattr(agent, "_budget_tracker", None)
        if tracker is None or not tracker.is_exceeded():
            return None
        return {
            "blocked": True,
            "reason": (
                f"Token budget exceeded: {tracker.spent:,} / {tracker.total:,} tokens used. "
                f"Set a larger budget via `/budget <n>`, or disable enforcement "
                f"by setting BUDGET_ENFORCE=false."
            ),
            "error_code": "BUDGET_EXCEEDED",
        }

    def on_model_changed(
        self, agent: "CodeAgent", old_model: str, new_model: str
    ) -> None:
        """Log model change so the user knows budget tracking continues
        across the swap. Different models may have different per-token
        pricing, so the remaining-token count translates to a different
        dollar cost — but we don't reset the budget here (that's a
        deliberate user decision, exposed via /budget)."""
        if old_model == new_model:
            return
        tracker = getattr(agent, "_budget_tracker", None)
        if tracker is None or tracker.total is None:
            return
        agent.logger.info(
            "Budget unchanged across model swap %s → %s (%s / %s tokens still tracked; "
            "note: per-token pricing may differ)",
            old_model, new_model, tracker.spent, tracker.total,
        )


class PlanModeMiddleware:
    """Switches the agent into a read-only planning mode.

    Only a subset of tools are available.  The plan text is injected
    into the next system prompt on exit.
    """

    name = "plan_mode"
    order = 60

    PLAN_MODE_TOOLS = {
        "Read", "Grep", "Glob", "LS", "TodoWrite",
        "TaskOutput", "EnterPlanMode", "ExitPlanMode", "AskUser",
        "ToolSearch",
    }

    def start(self, agent: "CodeAgent") -> None:
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
