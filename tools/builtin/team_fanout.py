"""TeamFanout tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.team_engine.manager import TeamManager, TeamManagerError
from ..base import ErrorCode, Tool, ToolParameter, map_team_error_code


class TeamFanoutTool(Tool):
    usage_notes = 'TeamFanout: Dispatch tasks to multiple teammates in parallel.'
    def __init__(
        self,
        name: str = "TeamFanout",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        team_manager: Optional[TeamManager] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description='Dispatch parallel work items to teammates.',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if team_manager is None:
            raise ValueError("team_manager is required")
        self._team_manager = team_manager

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="team_name", type="string", description="Team name", required=True),
            ToolParameter(
                name="tasks",
                type="array",
                description="Work items list. Each item: owner,title,instruction,payload(optional)",
                required=True,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        team_name = parameters.get("team_name")
        tasks = parameters.get("tasks")
        if not isinstance(team_name, str) or not team_name.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'team_name' is required and must be a non-empty string.",
                params_input=params_input,
            )
        if not isinstance(tasks, list) or not tasks:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'tasks' is required and must be a non-empty list.",
                params_input=params_input,
            )
        try:
            result = self._team_manager.fanout_work(team_name=team_name, tasks=tasks)
            return self.create_success_response(
                data=result,
                text=f"Fanout dispatched {len(result.get('work_items', []))} work items.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except TeamManagerError as exc:
            return self.create_error_response(
                error_code=map_team_error_code(exc.code),
                message=exc.message,
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"TeamFanout failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
