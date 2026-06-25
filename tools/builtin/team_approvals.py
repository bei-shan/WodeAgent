"""TeamApprovals tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.team_engine.manager import TeamManager, TeamManagerError
from ..base import ErrorCode, Tool, ToolParameter, map_team_error_code


class TeamApprovalsTool(Tool):
    usage_notes = 'TeamApprovals: View pending plan approvals for a team.'
    def __init__(
        self,
        name: str = "TeamApprovals",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        team_manager: Optional[TeamManager] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description='List plan-approval requests for a team.',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if team_manager is None:
            raise ValueError("team_manager is required")
        self._team_manager = team_manager

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="team_name", type="string", description="Team name", required=True),
            ToolParameter(name="status", type="string", description="Optional status filter", required=False),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        team_name = parameters.get("team_name")
        status = parameters.get("status")

        if not isinstance(team_name, str) or not team_name.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'team_name' is required and must be a non-empty string.",
                params_input=params_input,
            )
        if status is not None and not isinstance(status, str):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'status' must be a string when provided.",
                params_input=params_input,
            )

        try:
            items = self._team_manager.list_plan_approvals(team_name=team_name, status=status)
            return self.create_success_response(
                data={"items": items, "total": len(items)},
                text=f"Listed {len(items)} plan approvals.",
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
                message=f"TeamApprovals failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
