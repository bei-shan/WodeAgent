"""Drive an active goal across automatic continuation rounds.

Modelled on DeepSeek Harness' goal round driver: the driver only decides
whether the turn that just finished should be followed by another round. It
never creates a goal, never continues a terminal one, stops on cancellation or
failure, and respects both the goal's round budget and a per-invocation cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.goals import ACTIVE, Goal, GoalError, GoalStore

CONTINUATION_PROMPT = (
    "Continue working toward the active goal:\n{objective}\n\n"
    "Rules:\n"
    "- Make concrete progress this round; do not restate the plan.\n"
    "- If the objective is achieved, call Goal(action=\"complete\").\n"
    "- If you are blocked, call Goal(action=\"block\", reason=\"<concrete blocker>\").\n"
    "- Do not ask the user to confirm unless the objective is ambiguous."
)


@dataclass(frozen=True)
class Decision:
    continue_run: bool
    prompt: str = ""
    reason: str = ""


class GoalRoundDriver:
    def __init__(
        self,
        store: GoalStore,
        *,
        max_auto_rounds: int = 8,
        enabled: bool = True,
    ):
        self.store = store
        self.max_auto_rounds = max(0, int(max_auto_rounds))
        self.enabled = bool(enabled)

    def decide(
        self,
        session_id: str,
        *,
        turn_status: str = "completed",
        interrupted: bool = False,
        auto_rounds: int = 0,
    ) -> Decision:
        """Return whether another round should run, and the prompt for it.

        Consuming a round is a side effect: the budget is claimed before the
        round starts so two callers cannot overrun it.
        """
        if not self.enabled:
            return Decision(False, reason="autorun disabled")
        if not session_id:
            return Decision(False, reason="no session")
        try:
            goal: Optional[Goal] = self.store.get(session_id)
        except GoalError as exc:
            return Decision(False, reason=str(exc))
        if goal is None:
            return Decision(False, reason="no goal")
        if goal.status != ACTIVE:
            return Decision(False, reason=f"goal {goal.status}")
        if interrupted:
            return Decision(False, reason="interrupted")
        if turn_status != "completed":
            return Decision(False, reason=f"turn {turn_status}")
        if auto_rounds >= self.max_auto_rounds:
            return Decision(False, reason="auto round cap")
        if goal.rounds_started >= goal.max_rounds:
            return Decision(False, reason="round budget exhausted")
        try:
            goal = self.store.start_round(session_id)
        except GoalError as exc:
            return Decision(False, reason=str(exc))
        return Decision(True, prompt=CONTINUATION_PROMPT.format(objective=goal.objective))
