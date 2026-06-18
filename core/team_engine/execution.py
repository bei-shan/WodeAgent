"""Work-item execution service."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .turn_executor import TurnExecutor
from tools.registry import ToolRegistry

_ROLE_SYSTEM_PROMPTS: Dict[str, str] = {
    "developer": (
        "You are a developer teammate. Complete the assigned work item by "
        "using available tools. Produce working, correct code. "
        "Task recursion is forbidden."
    ),
    "reviewer": (
        "You are a reviewer teammate. Examine the assigned work item with "
        "a critical eye. Identify issues, suggest improvements, and validate "
        "correctness. Do NOT create or edit files. Task recursion is forbidden."
    ),
    "planner": (
        "You are a planner teammate. Analyze the assigned work item and "
        "produce a clear, actionable plan. Break down complex tasks into "
        "smaller steps. Do NOT create or edit files. Task recursion is forbidden."
    ),
}

_DEFAULT_WORKER_PROMPT = _ROLE_SYSTEM_PROMPTS["developer"]


class ExecutionService:
    """Runs teammate work items with tool-policy filtering."""

    def __init__(
        self,
        *,
        project_root: str,
        llm: Optional[Any],
        tool_registry: Optional[Any],
        work_executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
        read_team_fn: Callable[[str], Dict[str, Any]],
        llm_semaphore: Optional[Any] = None,
    ):
        self._project_root = project_root
        self._llm = llm
        self._tool_registry = tool_registry
        self._work_executor = work_executor
        self._read_team_fn = read_team_fn
        self._llm_semaphore = llm_semaphore

    def execute_work_item(self, team_name: str, teammate_name: str, work_item: Dict[str, Any]) -> Dict[str, Any]:
        if self._work_executor:
            payload = self._work_executor(dict(work_item))
            if isinstance(payload, dict):
                return payload
            return {"result": payload}
        if self._llm is None or self._tool_registry is None:
            return {"result": f"[{team_name}/{teammate_name}] completed: {work_item.get('title', '')}"}

        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                if self._llm_semaphore is None:
                    return self._run_turn_executor_work(team_name, teammate_name, work_item)
                with self._llm_semaphore:
                    return self._run_turn_executor_work(team_name, teammate_name, work_item)
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
                message = str(exc).lower()
                retryable = "rate limit" in message or "429" in message or "timeout" in message
                if not retryable or attempt >= 2:
                    break
                time.sleep(0.2 * (2 ** attempt))
        raise RuntimeError(str(last_error) if last_error else "work item execution failed")

    def _run_turn_executor_work(self, team_name: str, teammate_name: str, work_item: Dict[str, Any]) -> Dict[str, Any]:
        registry, denied_tools = self._build_teammate_registry(team_name, teammate_name)
        executor = TurnExecutor(
            llm=self._llm,
            tool_registry=registry,
            project_root=Path(self._project_root),
            denied_tools=denied_tools,
        )
        instruction = str(work_item.get("instruction") or "")
        role = self._get_teammate_role(team_name, teammate_name)
        system_prompt = _ROLE_SYSTEM_PROMPTS.get(role, _DEFAULT_WORKER_PROMPT)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instruction},
        ]
        tool_usage: Dict[str, int] = {}
        last_tool_msg = ""
        max_steps = int(os.getenv("TEAM_WORKER_MAX_STEPS", "8"))
        for _ in range(max(1, max_steps)):
            turn = executor.execute_turn(messages, tool_usage=tool_usage)
            messages = turn["messages"]
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            if tool_messages:
                last_tool_msg = str(tool_messages[-1].get("content", ""))
            if turn["done"]:
                final_result = str(turn.get("final_result") or "").strip()
                if final_result:
                    return {"result": final_result, "tool_usage": tool_usage}
                break
        if last_tool_msg:
            return {"result": last_tool_msg, "tool_usage": tool_usage}
        return {"result": "", "tool_usage": tool_usage}

    def _get_teammate_role(self, team_name: str, teammate_name: str) -> str:
        """Return the teammate's role from team config, defaulting to 'developer'."""
        try:
            cfg = self._read_team_fn(team_name)
            for member in cfg.get("members", []):
                if str(member.get("name") or "") == teammate_name:
                    return str(member.get("role") or "developer")
        except Exception:
            pass
        return "developer"

    def _build_teammate_registry(self, team_name: str, teammate_name: str) -> Tuple[ToolRegistry, Set[str]]:
        cfg = self._read_team_fn(team_name)
        teammate = None
        for member in cfg.get("members", []):
            if str(member.get("name") or "") == teammate_name:
                teammate = member
                break
        policy = (teammate or {}).get("tool_policy") if isinstance(teammate, dict) else {}
        if not isinstance(policy, dict):
            policy = {}
        allowlist = policy.get("allowlist")
        denylist = policy.get("denylist")
        allowset = {str(x) for x in allowlist} if isinstance(allowlist, list) else set()
        denyset = {str(x) for x in denylist} if isinstance(denylist, list) else set()
        denyset.add("Task")

        filtered = ToolRegistry()
        main_gate = None
        for tool in self._tool_registry.get_all_tools():
            name = tool.name
            if name in denyset:
                continue
            if allowset and name not in allowset:
                continue
            if main_gate is None:
                main_gate = getattr(tool, "_permission_gate", None)
            filtered.register_tool(tool)

        # Team workers inherit the main agent's authorization cache but
        # must not prompt the user directly.
        if main_gate is not None:
            sub_gate = main_gate.subagent_gate()
            for tool in filtered.get_all_tools():
                tool._permission_gate = sub_gate

        return filtered, denyset

