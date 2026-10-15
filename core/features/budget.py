"""Compatibility feature adapter for BudgetMiddleware.

Core enforcement lives in the mandatory PolicyMiddlewarePipeline.
"""
from __future__ import annotations

from core.features.base import AgentFeature
from core.runtime.policy_middleware import BudgetMiddleware


class BudgetFeature(AgentFeature):
    name = "budget"
    order = 55

    def init(self, agent):
        # Local standalone callers can still initialize this feature.
        # CodeAgent initializes all mandatory middleware before plugin loading.
        if getattr(agent, "_policy_middleware", None) is None:
            BudgetMiddleware().start(agent)

    def pre_tool_use(self, agent, tool_name, tool_input):
        # Legacy callers continue to use the public Feature API. The real
        # Agent dispatch checks the same policy before all plugin hooks.
        return BudgetMiddleware().pre_tool_use(agent, tool_name, tool_input)

    def runtime_blocks(self, agent, step):
        return BudgetMiddleware().runtime_blocks(agent, step)

    def on_model_changed(self, agent, old_model, new_model):
        return BudgetMiddleware().on_model_changed(agent, old_model, new_model)
