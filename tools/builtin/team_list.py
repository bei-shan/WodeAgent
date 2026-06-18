"""TeamList tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.team_engine.manager import TeamManager
from prompts.tools_prompts.team_list_prompt import team_list_prompt
from ..base import ErrorCode, Tool, ToolParameter


class TeamListTool(Tool):
    def __init__(
        self,
        name: str = "TeamList",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        team_manager: Optional[TeamManager] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description=team_list_prompt,
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if team_manager is None:
            raise ValueError("team_manager is required")
        self._team_manager = team_manager

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        try:
            names = self._team_manager.store.list_teams()
            summaries: List[Dict[str, Any]] = []
            for name in names:
                try:
                    status = self._team_manager.get_status(name)
                    worker_state = self._team_manager.worker_supervisor.team_state(name)
                    summaries.append({
                        "team_name": name,
                        "member_count": len(status.get("members", [])),
                        "active_teammates": worker_state.get("active_teammates", []),
                        "idle_teammates": worker_state.get("idle_teammates", []),
                        "stopped_teammates": worker_state.get("stopped_teammates", []),
                    })
                except Exception:
                    summaries.append({"team_name": name, "error": "unavailable"})
            return self.create_success_response(
                data={"teams": summaries},
                text=f"Found {len(summaries)} team(s).",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"TeamList failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
