"""Memory tool - explicit long-term memory access for the agent."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from core.memory.registry import MemoryProviderUnavailable
from core.memory.service import LocalMemoryService
from tools.base import ErrorCode, Tool, ToolParameter

ACTIONS = ("save", "search", "list", "delete")
DEFAULT_LIMIT = 8


class MemoryTool(Tool):
    strict_input_contract = True
    name = "Memory"
    description = "长期记忆：保存、检索和删除当前工作区的持久记忆。"
    usage_notes = (
        "Memory: explicit long-term memory for this workspace. Save durable facts "
        "worth recalling later (conventions, decisions, environment details), search "
        "before asking the user again, and delete entries that are wrong. Memory is "
        "off by default; when disabled, tell the user to enable it under 工作台 → 记忆 "
        "in the Web UI instead of retrying."
    )

    def __init__(self, project_root=None, working_dir=None):
        super().__init__(
            name=self.name,
            description=self.description,
            project_root=project_root,
            working_dir=working_dir,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="save / search / list / delete。",
                required=True,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="action=save 时要记住的内容（1-20000 字符）。",
                required=False,
            ),
            ToolParameter(
                name="query",
                type="string",
                description="action=search 时的检索词（字面子串匹配）。",
                required=False,
            ),
            ToolParameter(
                name="record_id",
                type="string",
                description="action=delete 时的记忆 id。",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="返回条数，默认 8，范围 1-20。",
                required=False,
                default=DEFAULT_LIMIT,
            ),
        ]

    def is_concurrency_safe(self, parameters: Dict[str, Any]) -> bool:
        return True

    @staticmethod
    def _render_records(records: list[dict[str, Any]], empty: str) -> str:
        if not records:
            return empty
        lines = [f"Memory ({len(records)}):"]
        for item in records:
            content = " ".join(str(item["content"]).split())
            lines.append(f"- [{item['id']}] {content[:300]}")
        return "\n".join(lines)

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
            limit = int(params_input.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(20, limit))

        root = self._project_root or self._working_dir or "."
        service = LocalMemoryService(root)
        if not service.enabled:
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=(
                    "长期记忆未启用：请在 Web UI「工作台 → 记忆」打开，"
                    "并选择 sqlite 后端。"
                ),
                params_input=params_input,
                time_ms=0,
            )

        try:
            if action == "save":
                content = str(params_input.get("content") or "").strip()
                if not content:
                    return self.create_error_response(
                        error_code=ErrorCode.INVALID_PARAM,
                        message="content 不能为空。",
                        params_input=params_input,
                        time_ms=int((time.monotonic() - start) * 1000),
                    )
                record = service.add(content)
                payload = LocalMemoryService.record_to_dict(record)
                return self.create_success_response(
                    data={"record": payload},
                    text=f"Saved memory [{payload['id']}]",
                    params_input=params_input,
                    time_ms=int((time.monotonic() - start) * 1000),
                )

            if action == "search":
                query = str(params_input.get("query") or "").strip()
                records = service.search(query, limit=limit)
                payload = [LocalMemoryService.record_to_dict(item) for item in records]
                return self.create_success_response(
                    data={"records": payload},
                    text=self._render_records(payload, "No matching memories."),
                    params_input=params_input,
                    time_ms=int((time.monotonic() - start) * 1000),
                )

            if action == "list":
                records = service.list(limit=limit)
                payload = [LocalMemoryService.record_to_dict(item) for item in records]
                return self.create_success_response(
                    data={"records": payload},
                    text=self._render_records(payload, "No memories stored yet."),
                    params_input=params_input,
                    time_ms=int((time.monotonic() - start) * 1000),
                )

            record_id = str(params_input.get("record_id") or "").strip()
            deleted = service.delete(record_id)
            return self.create_success_response(
                data={"deleted": deleted, "record_id": record_id},
                text="Memory deleted." if deleted else "No memory with that id.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start) * 1000),
            )
        except MemoryProviderUnavailable as exc:
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=str(exc),
                params_input=params_input,
                time_ms=int((time.monotonic() - start) * 1000),
            )
        except ValueError as exc:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=str(exc),
                params_input=params_input,
                time_ms=int((time.monotonic() - start) * 1000),
            )
