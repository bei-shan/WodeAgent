"""AgentTeamsFeature — multi-agent team collaboration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class AgentTeamsFeature(AgentFeature):
    """Manages AgentTeams: team creation, messaging, task board, plan approvals."""

    name = "agent_teams"
    order = 30

    def init(self, agent: "CodeAgent") -> None:
        agent.enable_agent_teams = bool(
            getattr(agent.config, "enable_agent_teams", False)
        )
        agent.team_store_dir = str(
            getattr(agent.config, "agent_teams_store_dir", ".teams") or ".teams"
        )
        agent.task_store_dir = str(
            getattr(agent.config, "agent_tasks_store_dir", ".tasks") or ".tasks"
        )
        agent.team_manager = None

        if agent.enable_agent_teams:
            try:
                from core.team_engine.manager import TeamManager
                from core.team_engine.display_mode import resolve_teammate_mode

                teammate_mode = str(
                    getattr(agent.config, "teammate_mode", "auto") or "auto"
                )
                runtime_mode, warning = resolve_teammate_mode(teammate_mode)
                if warning:
                    agent.logger.warning(warning)

                agent.team_manager = TeamManager(
                    project_root=agent.project_root,
                    team_store_dir=agent.team_store_dir,
                    task_store_dir=agent.task_store_dir,
                    llm=agent.llm,
                    tool_registry=agent.tool_registry,
                    teammate_runtime_mode=runtime_mode,
                )
            except Exception as exc:
                agent.logger.warning(
                    "Failed to initialize TeamManager, AgentTeams disabled: %s", exc
                )
                agent.enable_agent_teams = False

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        if not agent.enable_agent_teams or not agent.team_manager:
            return []
        if not hasattr(agent.context_builder, "set_runtime_system_blocks"):
            return []

        events = agent.team_manager.drain_events()
        runtime_state = agent.team_manager.export_state()
        return agent._format_runtime_system_blocks(
            events, runtime_state=runtime_state
        )

    def cleanup(self, agent: "CodeAgent") -> None:
        """Shut down the team manager if one was created at init."""
        manager = getattr(agent, "team_manager", None)
        if manager is None:
            return
        try:
            manager.shutdown()
        except Exception as exc:
            agent.logger.warning("TeamManager shutdown failed: %s", exc)
