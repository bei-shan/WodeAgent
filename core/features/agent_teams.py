"""Compatibility adapter: real resource ownership is in core.runtime.service_teams."""
from __future__ import annotations

from core.features.base import AgentFeature
from core.runtime.service_teams import TeamService


class AgentTeamsFeature(AgentFeature):
    """Legacy AgentFeature bridge. RuntimeServices owns the live instance."""
    name = "agent_teams"
    order = 30

    def _resolve(self, agent):
        graph = getattr(agent, "_runtime_services", None)
        if graph is not None:
            return graph.ensure("teams")
        service = getattr(self, "_standalone_service", None)
        if service is None:
            service = TeamService()
            self._standalone_service = service
            service.start(agent)
        return service

    def init(self, agent):
        self._resolve(agent)

    def cleanup(self, agent):
        if getattr(agent, "_runtime_services", None) is None:
            (getattr(self, "_standalone_service", None) or TeamService()).close(agent)

    def runtime_blocks(self, agent, step):
        return self._resolve(agent).runtime_blocks(agent, step)
