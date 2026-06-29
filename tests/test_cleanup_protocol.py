"""AgentFeature.cleanup() protocol activation tests.

Verifies that CodeAgent.close():
- Iterates reversed(self._features) calling cleanup(self) on each
- Swallows per-feature exceptions and continues the chain
- Is idempotent (close twice = no crash, work runs once)
- Calls trace_logger.finalize() as framework teardown

Also sanity-checks that the relocated MCP-close and team-shutdown logic
lives in the right feature's cleanup() now.

Run:
    python -m pytest tests/test_cleanup_protocol.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.features.agent_teams import AgentTeamsFeature
from core.features.base import AgentFeature
from core.features.mcp import MCPFeature


# ---------------------------------------------------------------------------
# MCPFeature.cleanup: closes every mcp client
# ---------------------------------------------------------------------------


def test_mcp_feature_cleanup_closes_all_clients():
    """Every client in agent._mcp_clients gets close_sync() — even if
    an earlier one raises."""
    c1 = MagicMock()
    c2_broken = MagicMock()
    c2_broken.close_sync.side_effect = RuntimeError("kaboom")
    c3 = MagicMock()
    agent = SimpleNamespace(_mcp_clients=[c1, c2_broken, c3])

    MCPFeature().cleanup(agent)

    c1.close_sync.assert_called_once()
    c2_broken.close_sync.assert_called_once()
    c3.close_sync.assert_called_once()  # broken sibling did not block


def test_mcp_feature_cleanup_no_clients_does_not_crash():
    """Missing or empty _mcp_clients must not raise."""
    MCPFeature().cleanup(SimpleNamespace(_mcp_clients=[]))
    MCPFeature().cleanup(SimpleNamespace())  # attr absent


# ---------------------------------------------------------------------------
# AgentTeamsFeature.cleanup: shuts down team_manager
# ---------------------------------------------------------------------------


def test_agent_teams_feature_cleanup_shuts_down_team_manager():
    """team_manager.shutdown() is called when one was created."""
    mgr = MagicMock()
    agent = SimpleNamespace(team_manager=mgr, logger=MagicMock())

    AgentTeamsFeature().cleanup(agent)
    mgr.shutdown.assert_called_once()


def test_agent_teams_feature_cleanup_swallows_shutdown_error():
    """If team_manager.shutdown raises, cleanup logs a warning but does
    not propagate."""
    mgr = MagicMock()
    mgr.shutdown.side_effect = RuntimeError("ts failure")
    agent = SimpleNamespace(team_manager=mgr, logger=MagicMock())

    AgentTeamsFeature().cleanup(agent)  # no exception
    agent.logger.warning.assert_called_once()


def test_agent_teams_feature_cleanup_no_manager_does_not_crash():
    """team_manager=None or missing must be a no-op."""
    AgentTeamsFeature().cleanup(SimpleNamespace(team_manager=None, logger=MagicMock()))
    AgentTeamsFeature().cleanup(SimpleNamespace(logger=MagicMock()))


# ---------------------------------------------------------------------------
# Dispatch contract: CodeAgent.close iterates reversed features (LIFO),
# swallows per-feature errors, and is idempotent.
# ---------------------------------------------------------------------------


def _make_close_test_agent(features):
    """Stand-in object exposing the bits CodeAgent.close needs.

    We can't easily instantiate CodeAgent in a unit test (it pulls in
    LLM client, model_profiles, history_manager, etc.). Instead we
    invoke the bound close() method against a minimal agent shape.
    """
    from agents.codeAgent import CodeAgent

    agent = SimpleNamespace(
        _features=features,
        logger=MagicMock(),
        trace_logger=MagicMock(),
    )
    # Borrow CodeAgent.close as a free function and bind to our stand-in.
    return agent, CodeAgent.close.__get__(agent)


def test_close_iterates_features_in_reverse_order():
    """Features are called LIFO — last-init, first-cleanup."""
    call_order = []

    class TrackedFeature(AgentFeature):
        def __init__(self, marker):
            self._marker = marker
            self.name = f"tracked_{marker}"

        def cleanup(self, agent):
            call_order.append(self._marker)

    feats = [TrackedFeature("first"), TrackedFeature("second"), TrackedFeature("third")]
    agent, close = _make_close_test_agent(feats)
    close()

    assert call_order == ["third", "second", "first"]


def test_close_swallows_per_feature_errors():
    """One feature raising must not stop subsequent cleanups."""
    calls = []

    class Failing(AgentFeature):
        name = "failing"
        def cleanup(self, agent):
            calls.append("failing")
            raise RuntimeError("boom")

    class OK(AgentFeature):
        name = "ok"
        def cleanup(self, agent):
            calls.append("ok")

    agent, close = _make_close_test_agent([OK(), Failing(), OK()])
    close()

    assert calls == ["ok", "failing", "ok"]
    # Warning logged for the failure (plus possibly trace finalize)
    assert any("failing" in str(c) for c in agent.logger.warning.call_args_list)


def test_close_finalizes_trace_logger():
    """Framework teardown: trace_logger.finalize() is called and the
    attribute is nulled out so a second close() doesn't re-finalize."""
    agent, close = _make_close_test_agent([])
    trace = agent.trace_logger
    close()

    trace.finalize.assert_called_once()
    assert agent.trace_logger is None


def test_close_is_idempotent():
    """Second close() call must be a no-op (no double-save, no double-
    finalize)."""
    calls = []

    class CountedFeature(AgentFeature):
        name = "counted"
        def cleanup(self, agent):
            calls.append("cleanup")

    agent, close = _make_close_test_agent([CountedFeature()])
    trace = agent.trace_logger

    close()
    close()  # second call — must be a no-op

    assert calls == ["cleanup"]  # only fired once
    trace.finalize.assert_called_once()


def test_close_handles_missing_features_list():
    """Defensive: if _features is unset or empty, close() still finalizes
    trace_logger without crashing."""
    from agents.codeAgent import CodeAgent

    agent = SimpleNamespace(logger=MagicMock(), trace_logger=MagicMock())
    trace = agent.trace_logger  # capture before close() nulls it out
    # _features attr is intentionally absent.
    CodeAgent.close.__get__(agent)()
    trace.finalize.assert_called_once()
