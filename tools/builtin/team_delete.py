"""TeamDelete tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.team_engine.manager import TeamManager, TeamManagerError
from ..base import ErrorCode, Tool, ToolParameter, map_team_error_code


class TeamDeleteTool(Tool):
    usage_notes = 'TeamDelete: Delete a team and all its data.'
    def __init__(
        self,
        name: str = "TeamDelete",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        team_manager: Optional[TeamManager] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description='Shutdown and delete a team runtime and persisted state.',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if team_manager is None:
            raise ValueError("team_manager is required")
        self._team_manager = team_manager

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="team_name", type="string", description="Team name", required=True)]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        team_name = parameters.get("team_name")
        if not isinstance(team_name, str) or not team_name.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'team_name' is required and must be a non-empty string.",
                params_input=params_input,
            )
        try:
            result = self._team_manager.delete_team(team_name=team_name)
            return self.create_success_response(
                data=result,
                text=f"Team '{result.get('team_name')}' deleted.",
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
                message=f"TeamDelete failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

