"""Framework-owned, ordered tool-policy middleware, separate from plugin hooks.

Plan, Budget and Delegate rules execute before any third-party pre-tool
interceptor. Middleware is installed once per Agent and cannot be replaced
through AgentFeature or the plugin loader.
"""
from __future__ import annotations

from typing import Any

from core.runtime.policy_middleware import (
    DelegateModeMiddleware, BudgetMiddleware, PlanModeMiddleware,
)


class PolicyMiddlewarePipeline:
    """Mandatory core policy chain; exceptions deny tool dispatch."""

    def __init__(self, agent: Any):
        self._agent = agent
        self._policies = (
            DelegateModeMiddleware(),
            BudgetMiddleware(),
            PlanModeMiddleware(),
        )
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            raise RuntimeError("Core policies already initialized")
        # These policy adapters only initialize in-memory runtime state;
        # long-lived resources are managed by RuntimeServices instead.
        for policy in self._policies:
            policy.start(self._agent)
        self._ready = True

    @property
    def policies(self):
        """Immutable core middleware order for context/status contributions."""
        return self._policies

    def on_model_changed(self, agent: Any, old_model: str, new_model: str) -> None:
        for policy in self._policies:
            observer = getattr(policy, "on_model_changed", None)
            if callable(observer):
                observer(agent, old_model, new_model)

    def before_tool(self, tool_name: str, parameters: dict) -> dict | None:
        if not self._ready:
            return {
                "blocked": True, "error_code": "PERMISSION_DENIED",
                "reason": "Core tool policies are not ready.",
            }
        for policy in self._policies:
            try:
                result = policy.pre_tool_use(self._agent, tool_name, parameters)
            except Exception:
                return {
                    "blocked": True, "error_code": "PERMISSION_DENIED",
                    "reason": "Core tool policy unavailable.",
                }
            if result and result.get("blocked"):
                return result
        return None
