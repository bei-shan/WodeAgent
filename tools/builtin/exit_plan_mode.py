"""ExitPlanMode tool — exit read-only planning mode, inject plan into context."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from prompts.tools_prompts.exit_plan_mode_prompt import exit_plan_mode_prompt
from ..base import ErrorCode, Tool, ToolParameter


class ExitPlanModeTool(Tool):
    """Exit plan-only mode and restore full tool access."""

    def __init__(
        self,
        name: str = "ExitPlanMode",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        code_agent: Optional[Any] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description=exit_plan_mode_prompt,
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if code_agent is None:
            raise ValueError("code_agent is required")
        self._code_agent = code_agent

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="plan",
                type="string",
                description="The plan produced during plan mode. Injected into the system prompt.",
                required=True,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        plan = parameters.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'plan' is required and must be a non-empty string.",
                params_input=params_input,
            )
        try:
            self._code_agent.exit_plan_mode(plan=plan.strip())
            return self.create_success_response(
                data={"mode": "normal", "plan_length": len(plan.strip())},
                text="Exited plan mode. Full tool access restored. Plan has been injected into context.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"ExitPlanMode failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
