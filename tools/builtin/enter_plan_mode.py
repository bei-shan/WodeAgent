"""EnterPlanMode tool — switch to read-only planning mode."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import ErrorCode, Tool, ToolParameter


class EnterPlanModeTool(Tool):
    """Switch the agent into plan-only mode (read-only tools only)."""
    usage_notes = 'EnterPlanMode: Enter read-only analysis mode. Only Read/Grep/Glob/LS/TodoWrite available. Analyse the codebase and produce a plan, then call ExitPlanMode with your plan.'

    def __init__(
        self,
        name: str = "EnterPlanMode",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        code_agent: Optional[Any] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description='Switch the agent into plan-only mode.  In this mode only **read-only**',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if code_agent is None:
            raise ValueError("code_agent is required")
        self._code_agent = code_agent

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        try:
            self._code_agent.enter_plan_mode()
            return self.create_success_response(
                data={"mode": "plan"},
                text="Entered plan mode. Only read-only tools are available. "
                     "Analyse and produce a plan, then call ExitPlanMode.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"EnterPlanMode failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
