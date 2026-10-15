"""Compatibility feature adapter for PlanModeMiddleware.

Core enforcement lives in the mandatory PolicyMiddlewarePipeline.
"""
from __future__ import annotations

from core.features.base import AgentFeature
from core.runtime.policy_middleware import PlanModeMiddleware


class PlanModeFeature(AgentFeature):
    name = "plan_mode"
    order = 60
    PLAN_MODE_TOOLS = PlanModeMiddleware.PLAN_MODE_TOOLS

    def init(self, agent):
        # Local standalone callers can still initialize this feature.
        # CodeAgent initializes all mandatory middleware before plugin loading.
        if getattr(agent, "_policy_middleware", None) is None:
            PlanModeMiddleware().start(agent)

    def pre_tool_use(self, agent, tool_name, tool_input):
        # Legacy callers continue to use the public Feature API. The real
        # Agent dispatch checks the same policy before all plugin hooks.
        return PlanModeMiddleware().pre_tool_use(agent, tool_name, tool_input)

    def runtime_blocks(self, agent, step):
        return PlanModeMiddleware().runtime_blocks(agent, step)
