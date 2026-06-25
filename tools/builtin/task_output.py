"""TaskOutput tool — retrieve results of background sub-agent tasks."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.background_task import BackgroundTaskRunner
from prompts.tools_prompts.task_output_prompt import task_output_prompt
from ..base import ErrorCode, Tool, ToolParameter


class TaskOutputTool(Tool):
    """Retrieve results of a background task."""
    usage_notes = 'TaskOutput: Fetch results from a background task. Use run_in_background=true on Task first.'

    def __init__(
        self,
        name: str = "TaskOutput",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        background_runner: Optional[BackgroundTaskRunner] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description=task_output_prompt,
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if background_runner is None:
            raise ValueError("background_runner is required")
        self._runner = background_runner

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="task_id",
                type="string",
                description="The task ID returned by Task(run_in_background=true).",
                required=True,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        task_id = parameters.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'task_id' is required.",
                params_input=params_input,
            )

        status = self._runner.get_status(task_id)
        if status == "not_found":
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Background task '{task_id}' not found.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

        if status == "running":
            tasks = self._runner.list_all()
            elapsed = 0.0
            for t in tasks:
                if t["task_id"] == task_id:
                    elapsed = t.get("elapsed", 0)
                    break
            return self.create_success_response(
                data={"task_id": task_id, "status": "running", "elapsed": elapsed},
                text=f"Task '{task_id}' is still running ({elapsed:.0f}s).",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

        result = self._runner.get_result(task_id)
        if result is None:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Task '{task_id}' result file missing.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

        task_result = result.get("result", "")
        tool_usage = result.get("tool_usage", {})
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return self.create_success_response(
            data={
                "task_id": task_id,
                "status": result.get("status"),
                "result": task_result,
                "tool_usage": tool_usage,
                "finished_at": result.get("finished_at"),
            },
            text=f"Task '{task_id}' {result.get('status')}.\n\n{task_result}",
            params_input=params_input,
            time_ms=elapsed_ms,
            extra_stats={
                "tool_calls": sum(tool_usage.values()) if isinstance(tool_usage, dict) else 0,
            },
        )
