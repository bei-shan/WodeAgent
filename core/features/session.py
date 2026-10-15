"""Compatibility adapter: real resource ownership is in core.runtime.service_sessions."""
from __future__ import annotations

from core.features.base import AgentFeature
from core.runtime.service_sessions import SessionService


class SessionFeature(AgentFeature):
    """Legacy AgentFeature bridge. RuntimeServices owns the live instance."""
    name = "session"
    order = 100

    def _resolve(self, agent):
        graph = getattr(agent, "_runtime_services", None)
        if graph is not None:
            return graph.ensure("session")
        service = getattr(self, "_standalone_service", None)
        if service is None:
            service = SessionService()
            self._standalone_service = service
            service.start(agent)
        return service

    def init(self, agent):
        self._resolve(agent)

    def cleanup(self, agent):
        if getattr(agent, "_runtime_services", None) is None:
            (getattr(self, "_standalone_service", None) or SessionService()).close(agent)

    def on_model_changed(self, agent, old_model, new_model):
        return self._resolve(agent).on_model_changed(agent, old_model, new_model)
