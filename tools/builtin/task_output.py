"""TaskOutput tool — retrieve results of background sub-agent tasks."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F811

from core.background_task import BackgroundTaskRunner
from ..base import ErrorCode, Tool, ToolParameter


class TaskOutputTool(Tool):
    strict_input_contract = True
    MAX_RESULT_CHARS = 20000
    MAX_EVENT_CHARS = 500
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
            description='Retrieve the result of a background task launched with',
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
        info = self._runner.get_rich_status(task_id)
        if not isinstance(info, dict):
            info = {}

        if status == "not_found":
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Background task '{task_id}' not found.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

        if status == "running":
            elapsed = info.get("elapsed", 0)
            data: dict[str, Any] = {
                "task_id": task_id,
                "status": "running",
                "elapsed": elapsed,
                "last_step": info.get("last_step"),
                "current_tool": info.get("current_tool"),
            }
            last = info.get("last_event")
            if isinstance(last, dict):
                data["last_event"] = {
                    "type": str(last.get("type", ""))[:80],
                    "content": str(last.get("content", ""))[:self.MAX_EVENT_CHARS],
                }
            else:
                last = None

            # Build readable summary for the LLM
            parts = [f"Task '{task_id}' is running ({elapsed:.0f}s)."]
            if data.get("last_step"):
                parts.append(f"At step {data['last_step']}.")
            if data.get("current_tool"):
                parts.append(f"Currently using tool: {data['current_tool']}.")
            if last:
                content = str(last.get("content", ""))[:200]
                if content:
                    parts.append(f"Last output: {content}")

            return self.create_success_response(
                data=data,
                text=" ".join(parts),
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

        raw_result = result.get("result", "")
        task_result = raw_result if isinstance(raw_result, str) else str(raw_result)
        result_truncated = len(task_result) > self.MAX_RESULT_CHARS
        task_result = task_result[:self.MAX_RESULT_CHARS]
        tool_usage = result.get("tool_usage", {})
        if not isinstance(tool_usage, dict):
            tool_usage = {}
        # A corrupt/third-party status payload must not crash aggregation.
        tool_calls = sum(v for v in tool_usage.values() if type(v) is int and v >= 0)
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        data = {
            "task_id": task_id,
            "status": result.get("status"),
            "result": task_result,
            "result_truncated": result_truncated,
            "tool_usage": tool_usage,
            "finished_at": result.get("finished_at"),
        }
        error = result.get("error")
        if isinstance(error, dict):
            data["error"] = {"kind": error.get("kind"), "retryable": error.get("retryable", False)}

        return self.create_success_response(
            data=data,
            text=(
                f"Task '{task_id}' {result.get('status')}.\n\n{task_result}"
                + ("\n[Result truncated: inspect task artifacts for complete output.]"
                   if result_truncated else "")
            ),
            params_input=params_input,
            time_ms=elapsed_ms,
            extra_stats={
                "tool_calls": tool_calls,
            },
        )
