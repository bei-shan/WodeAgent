"""Feature registry — collects all built-in and plugin features."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.features.base import AgentFeature
from core.features.worktree import WorktreeFeature
from core.features.mcp import MCPFeature
from core.features.agent_teams import AgentTeamsFeature
from core.features.delegate import DelegateModeFeature
from core.features.plan_mode import PlanModeFeature
from core.features.background_task import BackgroundTaskFeature
from core.features.output_style import OutputStyleFeature
from core.features.hooks import HookFeature
from core.features.vcr import VCRFeature
from core.features.session import SessionFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


# All built-in features in initialisation order.
BUILTIN_FEATURES: list[type[AgentFeature]] = [
    WorktreeFeature,       # 20
    MCPFeature,            # 25
    AgentTeamsFeature,     # 30
    DelegateModeFeature,   # 40
    PlanModeFeature,       # 60
    BackgroundTaskFeature, # 70
    OutputStyleFeature,    # 80
    HookFeature,           # 85
    VCRFeature,            # 90
    SessionFeature,        # 100
]


def collect_all_features(agent: "CodeAgent") -> list[AgentFeature]:
    """Instantiate and return all features (built-in + plugin), sorted by order."""
    features: list[AgentFeature] = [cls() for cls in BUILTIN_FEATURES]
    # Plugin discovery (Phase 4): features.extend(_discover_plugins(agent))
    features.sort(key=lambda f: f.order)
    return features
