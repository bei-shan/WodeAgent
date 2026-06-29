"""BudgetFeature — token budget tracking and status display."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class BudgetFeature(AgentFeature):
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

    def init(self, agent: "CodeAgent") -> None:
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
