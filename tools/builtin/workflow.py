"""Workflow tool - bounded fan-out and pipelining over subagents."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from core.workflow import (
    MAX_CONCURRENCY_LIMIT,
    Runner,
    TaskSpec,
    Workflow,
    WorkflowError,
    WorkflowResult,
)
from tools.base import ErrorCode, Tool, ToolParameter

MODES = ("parallel", "pipeline")
DEFAULT_CONCURRENCY = 4


class WorkflowTool(Tool):
    strict_input_contract = True
    name = "Workflow"
    timeout_seconds = 1800.0
    description = "并行/流水线编排多个 subagent 任务，返回逐项结果。"
    usage_notes = (
        "Workflow: fan out independent subagent work instead of calling Task in a "
        "loop. mode=parallel runs tasks concurrently; mode=pipeline runs every "
        "stage for each item, where stage prompts may use {item} and {prev}. "
        "A failing task never cancels the others; results are per task. Keep "
        "max_concurrency small (default 4) unless the work is I/O bound."
    )

    def __init__(
        self,
        project_root=None,
        working_dir=None,
        code_agent=None,
        runner: Optional[Runner] = None,
    ):
        super().__init__(
            name=self.name,
            description=self.description,
            project_root=project_root,
            working_dir=working_dir,
        )
        self._agent = code_agent
        self._runner = runner

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="mode",
                type="string",
                description="parallel（默认）或 pipeline。",
                required=False,
                default="parallel",
            ),
            ToolParameter(
                name="tasks",
                type="array",
                description="parallel 模式的任务列表，每项 {id, prompt, agent_type?}。",
                required=False,
            ),
            ToolParameter(
                name="items",
                type="array",
                description="pipeline 模式的输入项（字符串列表）。",
                required=False,
            ),
            ToolParameter(
                name="stages",
                type="array",
                description="pipeline 模式的阶段列表，每项 {id, prompt, agent_type?}；prompt 可用 {item} 和 {prev}。",
                required=False,
            ),
            ToolParameter(
                name="max_concurrency",
                type="integer",
                description=f"并发上限，默认 {DEFAULT_CONCURRENCY}，最大 {MAX_CONCURRENCY_LIMIT}。",
                required=False,
                default=DEFAULT_CONCURRENCY,
            ),
        ]

    # ------------------------------------------------------------------
    def _subagent_runner(self, prompt: str, agent_type: str) -> str:
        """Default runner: the existing Task tool, one synchronous subagent."""
        registry = getattr(self._agent, "tool_registry", None)
        if registry is None:
            raise WorkflowError("Workflow requires an active agent with subagent support")
        raw = registry.execute_tool("Task", {
            "description": "workflow task",
            "prompt": prompt,
            "subagent_type": agent_type,
            "run_in_background": False,
        })
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return str(raw)
        if not isinstance(payload, dict):
            return str(raw)
        if payload.get("status") == "error":
            error = payload.get("error") or {}
            raise WorkflowError(str(error.get("message") or "subagent failed"))
        data = payload.get("data") or {}
        if isinstance(data, dict) and data.get("result") is not None:
            return str(data["result"])
        return str(payload.get("text") or "")

    @staticmethod
    def _clamp_concurrency(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_CONCURRENCY
        return max(1, min(MAX_CONCURRENCY_LIMIT, parsed))

    def run(self, parameters: Dict[str, Any]) -> str:
        params_input = parameters if isinstance(parameters, dict) else {}
        start = time.monotonic()
        mode = str(params_input.get("mode") or "parallel").strip().lower()
        if mode not in MODES:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"mode 必须是 {', '.join(MODES)} 之一。",
                params_input=params_input,
                time_ms=0,
            )

        runner = self._runner or self._subagent_runner
        concurrency = self._clamp_concurrency(params_input.get("max_concurrency"))
        workflow = Workflow(runner, max_concurrency=concurrency)
        try:
            if mode == "parallel":
                result = workflow.run_parallel(params_input.get("tasks") or [])
            else:
                result = workflow.run_pipeline(
                    params_input.get("items") or [], params_input.get("stages") or [],
                )
        except WorkflowError as exc:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=str(exc),
                params_input=params_input,
                time_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=f"Workflow failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start) * 1000),
            )

        time_ms = int((time.monotonic() - start) * 1000)
        return self.create_success_response(
            data=result.to_dict(),
            text=self._render(result),
            params_input=params_input,
            time_ms=time_ms,
        )

    @staticmethod
    def _render(result: WorkflowResult) -> str:
        lines = [f"Workflow {result.mode}: {result.completed}/{len(result.outcomes)} completed"]
        for outcome in result.outcomes:
            if outcome.status == "completed":
                lines.append(f"- {outcome.id}: completed ({len(outcome.result)} chars)")
            else:
                lines.append(f"- {outcome.id}: {outcome.status}: {outcome.error}")
        return "\n".join(lines)
