"""Goal tool - persist and advance a session objective across rounds."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

from core.goals import DEFAULT_MAX_ROUNDS, GoalError, GoalStore
from tools.base import ErrorCode, Tool, ToolParameter

ACTIONS = ("get", "set", "start_round", "complete", "block", "clear")


class GoalTool(Tool):
    strict_input_contract = True
    name = "Goal"
    description = "管理当前会话的长期目标（设置/查看/推进轮次/完成/阻塞/清除）。"
    usage_notes = (
        "Goal: use for objectives that outlive a single reply (multi-round work). "
        "Actions: set (objective, max_rounds), get, start_round (increments the "
        "budget), complete, block (concrete reason), clear. A goal is one per "
        "session; setting a new objective replaces it and resets the round count."
    )

    def __init__(self, project_root=None, working_dir=None, code_agent=None, store=None):
        super().__init__(
            name=self.name,
            description=self.description,
            project_root=project_root,
            working_dir=working_dir,
        )
        self._agent = code_agent
        self._store = store or GoalStore(self._goal_dir(project_root, working_dir))

    @staticmethod
    def _goal_dir(project_root, working_dir) -> Path:
        base = Path(project_root or working_dir or ".").resolve()
        return base / "memory" / "goals"

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作：get / set / start_round / complete / block / clear。",
                required=True,
            ),
            ToolParameter(
                name="objective",
                type="string",
                description="action=set 时的目标描述（可执行、可验证）。",
                required=False,
            ),
            ToolParameter(
                name="max_rounds",
                type="integer",
                description=f"action=set 时的轮次上限，默认 {DEFAULT_MAX_ROUNDS}，范围 1-50。",
                required=False,
                default=DEFAULT_MAX_ROUNDS,
            ),
            ToolParameter(
                name="reason",
                type="string",
                description="action=block 时的具体阻塞原因。",
                required=False,
            ),
        ]

    def _session_id(self) -> str:
        session_id = getattr(self._agent, "_session_id", None)
        if not session_id:
            raise GoalError("Goal requires an active session")
        return str(session_id)

    def run(self, parameters: Dict[str, Any]) -> str:
        params_input = parameters if isinstance(parameters, dict) else {}
        start = time.monotonic()
        action = str(params_input.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"action 必须是 {', '.join(ACTIONS)} 之一。",
                params_input=params_input,
                time_ms=0,
            )

        try:
            session_id = self._session_id()
            result = self._dispatch(action, session_id, params_input)
        except GoalError as exc:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=str(exc),
                params_input=params_input,
                time_ms=int((time.monotonic() - start) * 1000),
            )

        time_ms = int((time.monotonic() - start) * 1000)
        return self.create_success_response(
            data=result,
            text=self._render(action, result),
            params_input=params_input,
            time_ms=time_ms,
        )

    def _dispatch(self, action: str, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "get":
            goal = self._store.get(session_id)
            return {"goal": goal.to_dict() if goal else None}
        if action == "set":
            goal = self._store.set_objective(
                session_id,
                str(params.get("objective") or ""),
                max_rounds=int(params.get("max_rounds") or DEFAULT_MAX_ROUNDS),
            )
            return {"goal": goal.to_dict()}
        if action == "start_round":
            return {"goal": self._store.start_round(session_id).to_dict()}
        if action == "complete":
            return {"goal": self._store.complete(session_id).to_dict()}
        if action == "block":
            return {"goal": self._store.block(session_id, str(params.get("reason") or "")).to_dict()}
        cleared = self._store.clear(session_id)
        return {"cleared": cleared}

    @staticmethod
    def _render(action: str, result: Dict[str, Any]) -> str:
        if action == "clear":
            return "Goal cleared." if result.get("cleared") else "No goal to clear."
        goal = result.get("goal")
        if not goal:
            return "No goal is set for this session."
        return (
            f"Goal [{goal['status']}] {goal['objective']}\n"
            f"rounds: {goal['rounds_started']}/{goal['max_rounds']}"
            + (f"\nblocked: {goal['blocked_reason']}" if goal.get("blocked_reason") else "")
        )
