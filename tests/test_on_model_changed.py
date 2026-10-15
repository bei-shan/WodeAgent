"""on_model_changed lifecycle hook tests.

Verifies that CodeAgent.switch_model() dispatches the hook to every
feature, that reactors fire, and that errors in one reactor don't
block others.

Run:
    python -m pytest tests/test_on_model_changed.py -v
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.budget_tracker import BudgetTracker
from core.runtime.policy_middleware import BudgetMiddleware
from core.runtime.service_sessions import SessionService


# ---------------------------------------------------------------------------
# BudgetMiddleware reactor
# ---------------------------------------------------------------------------


def test_budget_feature_logs_on_model_change(caplog):
    """BudgetMiddleware.on_model_changed logs an info message when the
    model actually changed and a budget is set."""
    tracker = BudgetTracker(total=10_000)
    tracker._spent = 2500
    agent = SimpleNamespace(_budget_tracker=tracker, logger=logging.getLogger("test.budget"))

    feat = BudgetMiddleware()
    with caplog.at_level(logging.INFO, logger="test.budget"):
        feat.on_model_changed(agent, "old-model", "new-model")

    msgs = [r.getMessage() for r in caplog.records]
    assert any("old-model → new-model" in m for m in msgs)
    assert any("2500" in m for m in msgs)


def test_budget_feature_skips_when_model_unchanged():
    """No-op when old_model == new_model (defensive, switch_model
    might re-fire if the swap is idempotent)."""
    tracker = BudgetTracker(total=10_000)
    agent = SimpleNamespace(_budget_tracker=tracker, logger=MagicMock())
    feat = BudgetMiddleware()

    feat.on_model_changed(agent, "same", "same")
    agent.logger.info.assert_not_called()


def test_budget_feature_skips_when_no_budget():
    """No-op when no budget total is set (tracker.total is None)."""
    tracker = BudgetTracker(total=None)
    agent = SimpleNamespace(_budget_tracker=tracker, logger=MagicMock())
    feat = BudgetMiddleware()

    feat.on_model_changed(agent, "old", "new")
    agent.logger.info.assert_not_called()


# ---------------------------------------------------------------------------
# SessionService reactor
# ---------------------------------------------------------------------------


def test_session_feature_logs_to_trace_when_available():
    """SessionService.on_model_changed calls trace_logger.log_event."""
    trace = MagicMock()
    agent = SimpleNamespace(trace_logger=trace)

    feat = SessionService()
    feat.on_model_changed(agent, "claude-opus-4-8", "deepseek-chat")

    trace.log_event.assert_called_once_with(
        "model_change",
        {"old_model": "claude-opus-4-8", "new_model": "deepseek-chat"},
    )


def test_session_feature_no_trace_logger_does_not_crash():
    """If trace_logger is None or absent, the hook is a graceful no-op."""
    agent = SimpleNamespace()  # no trace_logger attr
    SessionService().on_model_changed(agent, "a", "b")  # no exception

    agent.trace_logger = None
    SessionService().on_model_changed(agent, "a", "b")  # no exception


def test_session_feature_swallows_trace_errors():
    """If trace_logger.log_event raises, SessionService must not propagate."""
    trace = MagicMock()
    trace.log_event.side_effect = RuntimeError("trace disk full")
    agent = SimpleNamespace(trace_logger=trace)
    SessionService().on_model_changed(agent, "a", "b")  # no exception


# ---------------------------------------------------------------------------
# Model changes are dispatched to explicit, typed middleware and services.
# ---------------------------------------------------------------------------


def test_core_middleware_and_session_observers_are_independent(caplog):
    from core.runtime.middleware import PolicyMiddlewarePipeline
    agent = SimpleNamespace(
        config=SimpleNamespace(budget_enforce=False, delegate_mode=False),
        logger=logging.getLogger("test.budget"),
        trace_logger=MagicMock(),
    )
    chain = PolicyMiddlewarePipeline(agent)
    chain.initialize()
    agent._budget_tracker.set_budget(2500)
    with caplog.at_level(logging.INFO, logger="test.budget"):
        chain.on_model_changed(agent, "model-a", "model-b")
    SessionService().on_model_changed(agent, "model-a", "model-b")
    agent.trace_logger.log_event.assert_called_once_with(
        "model_change", {"old_model": "model-a", "new_model": "model-b"},
    )
    assert any("model-a" in item.getMessage() for item in caplog.records)
