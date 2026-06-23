"""BudgetFeature — token budget tracking and status display."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class BudgetFeature(AgentFeature):
    """Tracks token consumption against an optional budget.

    Budget is parsed from user input (e.g. ``+500k``) and consumed
    each LLM call.  Status is injected into runtime blocks.
    """

    name = "budget"
    order = 55

    def init(self, agent: "CodeAgent") -> None:
        from core.budget_tracker import BudgetTracker

        agent._budget_tracker = BudgetTracker()

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        status = agent._budget_tracker.status_text()
        return [status] if status else []
