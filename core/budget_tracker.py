"""Token budget tracker — limit and track LLM token consumption.

Parses budget from user input (e.g. ``+500k``, ``预算 10 万``) and
tracks per-step usage.  Injects budget status into runtime blocks and
warns when approaching or exceeding the limit.

Usage::

    tracker = BudgetTracker()
    tracker.parse_from_input("重构 auth 模块 +500k")
    # ... each LLM call ...
    tracker.spend(usage["total_tokens"])
    # In runtime blocks:
    status = tracker.status_text()  # "Budget: 450000/500000 (90%)"
"""

from __future__ import annotations

import re
from typing import Optional


def parse_budget_from_input(user_input: str) -> int | None:
    """Extract token budget from user input.

    Supports:
    - ``+500k`` → 500000
    - ``+50万`` → 500000
    - ``预算 10 万`` → 100000
    - ``budget 200k`` → 200000
    - ``+1m`` → 1000000
    """
    if not user_input:
        return None

    # +500k / +1m
    m = re.search(r"\+(\d+)\s*([km])", user_input, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        unit = m.group(2).lower()
        return num * 1000 if unit == "k" else num * 1000000

    # +50万
    m = re.search(r"\+(\d+)\s*万", user_input)
    if m:
        return int(m.group(1)) * 10000

    # 预算 10 万 / budget 200k
    m = re.search(
        r"(?:预算|budget)\s*(\d+)\s*(万|[kKmM]?)",
        user_input,
        re.IGNORECASE,
    )
    if m:
        num = int(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "万":
            return num * 10000
        elif unit == "k":
            return num * 1000
        elif unit == "m":
            return num * 1000000
        return num

    return None


class BudgetTracker:
    """Tracks token consumption against an optional budget limit.

    Parameters
    ----------
    total:
        Total token budget.  ``None`` means unlimited.
    """

    def __init__(self, total: int | None = None):
        self.total = total
        self._spent: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def remaining(self) -> int | None:
        """Remaining token budget, or ``None`` if unlimited."""
        if self.total is None:
            return None
        return max(0, self.total - self._spent)

    @property
    def spent(self) -> int:
        return self._spent

    def spend(self, tokens: int) -> None:
        """Record token consumption."""
        self._spent += tokens

    def is_exceeded(self) -> bool:
        """Return ``True`` if the budget has been exceeded."""
        if self.total is None:
            return False
        return self._spent >= self.total

    def warning_level(self) -> str:
        """Return ``"ok"``, ``"warn"`` (>80%), or ``"critical"`` (>100%)."""
        if self.total is None:
            return "ok"
        pct = self._spent / self.total
        if pct >= 1.0:
            return "critical"
        if pct >= 0.8:
            return "warn"
        return "ok"

    def status_text(self) -> str:
        """Human-readable budget status for runtime blocks."""
        if self.total is None:
            return ""
        pct = (self._spent / self.total) * 100
        level = self.warning_level()
        prefix = "⚠️ " if level == "warn" else "🚫 " if level == "critical" else ""
        return (
            f"{prefix}Token Budget: {self._spent:,} / {self.total:,} "
            f"({pct:.0f}% used, {self.remaining:,} remaining)"
        )

    def set_budget(self, total: int | None) -> None:
        """Set or clear the budget."""
        self.total = total
        self._spent = 0

    def parse_from_input(self, user_input: str) -> bool:
        """Try to extract a budget from user input. Returns ``True`` if found."""
        budget = parse_budget_from_input(user_input)
        if budget is not None:
            self.set_budget(budget)
            return True
        return False
