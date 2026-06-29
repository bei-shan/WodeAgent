"""BudgetFeature.pre_tool_use enforce-mode tests.

Run:
    python -m pytest tests/test_budget_feature.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.budget_tracker import BudgetTracker
from core.exceptions import BudgetExceeded
from core.features.budget import BudgetFeature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(*, enforce: bool, budget_total: int | None, spent: int = 0) -> SimpleNamespace:
    """Build a minimal agent shaped to satisfy BudgetFeature.pre_tool_use."""
    tracker = BudgetTracker(total=budget_total)
    tracker._spent = spent  # type: ignore[attr-defined]
    return SimpleNamespace(
        config=SimpleNamespace(budget_enforce=enforce),
        _budget_tracker=tracker,
    )


# ---------------------------------------------------------------------------
# Enforce mode OFF (default): no blocking even when over budget
# ---------------------------------------------------------------------------


def test_pre_tool_use_default_does_not_block_when_disabled():
    """With budget_enforce=False (the default), pre_tool_use always
    returns None — even when the budget is exhausted."""
    agent = _make_agent(enforce=False, budget_total=1000, spent=9999)
    feat = BudgetFeature()

    result = feat.pre_tool_use(agent, "Read", {"file_path": "foo.py"})
    assert result is None  # never blocks


def test_pre_tool_use_does_not_block_when_under_budget():
    """Under budget → no block, regardless of enforce setting."""
    agent = _make_agent(enforce=True, budget_total=10_000, spent=500)
    feat = BudgetFeature()

    result = feat.pre_tool_use(agent, "Bash", {"command": "ls"})
    assert result is None


# ---------------------------------------------------------------------------
# Enforce mode ON: blocks when budget exceeded
# ---------------------------------------------------------------------------


def test_pre_tool_use_blocks_when_enforced_and_exceeded():
    """With budget_enforce=True and is_exceeded()=True, pre_tool_use
    returns the structured 'blocked' payload."""
    agent = _make_agent(enforce=True, budget_total=1000, spent=1500)
    feat = BudgetFeature()

    result = feat.pre_tool_use(agent, "Edit", {"file_path": "x.py", "old_string": "a", "new_string": "b"})
    assert result is not None
    assert result["blocked"] is True
    assert result["error_code"] == "BUDGET_EXCEEDED"
    assert "1,000" in result["reason"]  # locale-independent formatting check
    assert "1,500" in result["reason"]


def test_pre_tool_use_does_not_block_when_no_budget_set():
    """No budget total (unlimited) → is_exceeded() returns False, no block."""
    agent = _make_agent(enforce=True, budget_total=None, spent=999_999)
    feat = BudgetFeature()

    result = feat.pre_tool_use(agent, "Read", {})
    assert result is None


# ---------------------------------------------------------------------------
# Exception class
# ---------------------------------------------------------------------------


def test_budget_exceeded_exception_carries_counts():
    """BudgetExceeded exposes spent/total for callers that opt into
    fail-loud behavior."""
    exc = BudgetExceeded(spent=1500, total=1000)
    assert exc.spent == 1500
    assert exc.total == 1000
    msg = str(exc)
    assert "1,500" in msg
    assert "1,000" in msg
    assert "150%" in msg


# ---------------------------------------------------------------------------
# Defensive: missing tracker doesn't crash
# ---------------------------------------------------------------------------


def test_pre_tool_use_handles_missing_tracker():
    """If agent._budget_tracker is somehow absent, pre_tool_use must
    not raise — it just no-ops."""
    agent = SimpleNamespace(config=SimpleNamespace(budget_enforce=True))
    feat = BudgetFeature()
    result = feat.pre_tool_use(agent, "Read", {})
    assert result is None
