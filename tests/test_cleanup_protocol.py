"""Runtime services are sole resource owners; closing Agent is idempotent."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.runtime.service_mcp import MCPService
from core.runtime.service_teams import TeamService
from core.runtime.services import RuntimeServices


def test_mcp_service_closes_clients_even_if_sibling_fails():
    a, b, c = MagicMock(), MagicMock(), MagicMock()
    b.close_sync.side_effect = RuntimeError("cannot close")
    host = SimpleNamespace(_mcp_clients=[a, b, c])
    MCPService().close(host)
    a.close_sync.assert_called_once()
    b.close_sync.assert_called_once()
    c.close_sync.assert_called_once()


def test_team_service_closes_manager():
    mgr = MagicMock()
    agent = SimpleNamespace(team_manager=mgr, logger=MagicMock())
    TeamService().close(agent)
    mgr.shutdown.assert_called_once()


def test_team_service_does_not_hide_shutdown_errors_from_logger():
    mgr = MagicMock()
    mgr.shutdown.side_effect = RuntimeError("unavailable")
    agent = SimpleNamespace(team_manager=mgr, logger=MagicMock())
    TeamService().close(agent)
    agent.logger.warning.assert_called_once()


def test_codeagent_closes_service_owners_exactly_once():
    from agents.codeAgent import CodeAgent

    calls = []

    class First:
        def start(self, host):
            calls.append("first-start")
        def close(self, host):
            calls.append("first-close")

    class Second:
        def start(self, host):
            calls.append("second-start")
        def close(self, host):
            calls.append("second-close")

    agent = SimpleNamespace(logger=MagicMock(), trace_logger=MagicMock())
    graph = RuntimeServices(agent)
    graph.register("first", First)
    graph.register("second", Second, requires=("first",))
    graph.ensure("second")
    agent._runtime_services = graph
    close = CodeAgent.close.__get__(agent)
    close()
    close()
    assert calls == ["first-start", "second-start", "second-close", "first-close"]
    agent.trace_logger is None


def test_close_finalizes_trace_without_runtime():
    from agents.codeAgent import CodeAgent
    agent = SimpleNamespace(trace_logger=MagicMock(), logger=MagicMock())
    trace = agent.trace_logger
    CodeAgent.close.__get__(agent)()
    trace.finalize.assert_called_once()
    assert agent.trace_logger is None
