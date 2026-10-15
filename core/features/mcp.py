"""Compatibility adapter: real resource ownership is in core.runtime.service_mcp."""
from __future__ import annotations

from core.features.base import AgentFeature
from core.runtime.service_mcp import MCPService


class MCPFeature(AgentFeature):
    """Legacy AgentFeature bridge. RuntimeServices owns the live instance."""
    name = "mcp_status"
    order = 25

    def _resolve(self, agent):
        graph = getattr(agent, "_runtime_services", None)
        if graph is not None:
            return graph.ensure("mcp")
        service = getattr(self, "_standalone_service", None)
        if service is None:
            service = MCPService()
            self._standalone_service = service
            service.start(agent)
        return service

    def init(self, agent):
        self._resolve(agent)

    def cleanup(self, agent):
        if getattr(agent, "_runtime_services", None) is None:
            (getattr(self, "_standalone_service", None) or MCPService()).close(agent)

    def runtime_blocks(self, agent, step):
        return self._resolve(agent).runtime_blocks(agent, step)
