"""Compatibility feature adapter for DelegateModeMiddleware.

Core enforcement lives in the mandatory PolicyMiddlewarePipeline.
"""
from __future__ import annotations

from core.features.base import AgentFeature
from core.runtime.policy_middleware import DelegateModeMiddleware


class DelegateModeFeature(AgentFeature):
    name = "delegate_mode"
    order = 40
    DELEGATION_ALLOWED_TOOLS = DelegateModeMiddleware.DELEGATION_ALLOWED_TOOLS

    def init(self, agent):
        # Local standalone callers can still initialize this feature.
        # CodeAgent initializes all mandatory middleware before plugin loading.
        if getattr(agent, "_policy_middleware", None) is None:
            DelegateModeMiddleware().start(agent)

    def pre_tool_use(self, agent, tool_name, tool_input):
        # Legacy callers continue to use the public Feature API. The real
        # Agent dispatch checks the same policy before all plugin hooks.
        return DelegateModeMiddleware().pre_tool_use(agent, tool_name, tool_input)
