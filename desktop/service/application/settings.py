"""Session settings and Agent Teams orchestration; no web framework imports."""
from __future__ import annotations

import logging
from typing import Any

from core.runtime.session_controller import SessionController
from .errors import ConflictError, InvalidInputError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)


class SessionSettingsService:
    def __init__(self, controller: SessionController):
        self.controller = controller

    def _session(self, sid: str):
        session = self.controller.get_session(sid)
        if session is None:
            raise NotFoundError("Session not found")
        return session

    def get(self, sid: str) -> dict:
        agent = self._session(sid).ensure_agent()
        return {
            "model": getattr(agent.llm, "model", ""),
            "provider": getattr(agent.llm, "provider", ""),
            "enable_agent_teams": bool(getattr(agent, "enable_agent_teams", False)),
            "plan_mode": bool(getattr(agent, "_in_plan_mode", False)),
            "thinking_level": agent.history_manager.get_thinking_level(),
        }

    def update(
        self,
        sid: str,
        *,
        model_profile: str | None = None,
        enable_agent_teams: bool | None = None,
        plan_mode: bool | None = None,
    ) -> dict:
        session = self._session(sid)
        if session.busy:
            raise ConflictError("Cannot change configuration during a turn")
        agent = session.ensure_agent()
        if model_profile is not None:
            from core.model_profiles import load_model_profiles
            profiles = load_model_profiles()
            if model_profile == "default" and not profiles:
                pass
            elif model_profile not in profiles:
                raise InvalidInputError("Unknown model profile")
            else:
                agent.switch_model(model=model_profile)
        if enable_agent_teams is not None:
            self._set_teams_enabled(agent, bool(enable_agent_teams))
        if plan_mode is not None:
            if plan_mode:
                agent.enter_plan_mode()
            else:
                agent.exit_plan_mode("")
        return {"status": "updated"}

    @staticmethod
    def _set_teams_enabled(agent: Any, enabled: bool) -> None:
        # Construct using the SAME dependencies as AgentTeamsFeature.init.
        # A partial TeamManager must never be advertised as enabled.
        if enabled and getattr(agent, "team_manager", None) is None:
            from core.team_engine.manager import TeamManager
            from core.team_engine.display_mode import resolve_teammate_mode
            cfg = agent.config
            mode, warning = resolve_teammate_mode(
                str(getattr(cfg, "teammate_mode", "auto") or "auto")
            )
            if warning:
                logger.warning("Team runtime selection: %s", warning)
            try:
                manager = TeamManager(
                    project_root=agent.project_root,
                    team_store_dir=str(getattr(cfg, "agent_teams_store_dir", ".teams") or ".teams"),
                    task_store_dir=str(getattr(cfg, "agent_tasks_store_dir", ".tasks") or ".tasks"),
                    llm=agent.llm,
                    tool_registry=agent.tool_registry,
                    teammate_runtime_mode=mode,
                    config=agent.config,
                )
            except Exception as exc:
                logger.exception("TeamManager initialization failed")
                raise ServiceError("Unable to initialize Agent Teams") from exc
            agent.team_manager = manager
            bootstrap = getattr(agent, "_tool_bootstrap", None)
            if bootstrap is not None:
                bootstrap.provide("team_manager", manager)
                bootstrap.provide("event_sink", agent.event_sink)
                from core.tool_bootstrap import register_team_tools
                registered = register_team_tools(bootstrap)
                logger.info("Agent Teams enabled; registered %d team tools", len(registered))
            else:
                logger.warning("Agent has no ToolBootstrap; team tool registration skipped")
        agent.enable_agent_teams = enabled
        if hasattr(agent.config, "enable_agent_teams"):
            agent.config.enable_agent_teams = enabled


class TeamsService:
    def __init__(self, controller: SessionController):
        self.controller = controller

    def _live_agent(self, sid: str):
        session = self.controller.get_session(sid)
        return session.agent if session is not None else None

    def list(self, sid: str) -> list[dict]:
        agent = self._live_agent(sid)
        if agent is None or not getattr(agent, "enable_agent_teams", False):
            return []
        manager = getattr(agent, "team_manager", None)
        if manager is None:
            return []
        try:
            state = manager.export_state()
        except Exception:
            logger.exception("Failed to export Agent Teams state")
            return []
        teams = state.get("teams", {})
        result = []
        for name, info in teams.items():
            result.append({
                "name": name, "queued": info.get("queued", 0),
                "running": info.get("running", 0), "succeeded": info.get("succeeded", 0),
                "failed": info.get("failed", 0), "active": info.get("active", 0),
                "idle": info.get("idle", 0), "approvals_pending": info.get("approvals_pending", 0),
                "blocked": info.get("blocked", 0),
            })
        return result

    def create(self, sid: str, team_name: str) -> dict:
        session = self.controller.get_session(sid)
        if session is None:
            raise NotFoundError("Session not found")
        # The Agent is built lazily on first use, and creating a team is a use;
        # only a genuinely unknown session is a 404.
        agent = session.ensure_agent()
        manager = getattr(agent, "team_manager", None)
        if not getattr(agent, "enable_agent_teams", False) or manager is None:
            raise InvalidInputError("Agent teams not enabled. Toggle enable_agent_teams in session config first.")
        try:
            team = manager.create_team(team_name)
        except Exception as exc:
            logger.exception("Unable to create team")
            raise ServiceError("Unable to create team") from exc
        return {"status": "created", "team": team.get("name", team_name)}
