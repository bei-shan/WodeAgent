"""EnterWorktree tool — create or enter a git worktree for session isolation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.worktree.manager import WorktreeError, WorktreeManager
from ..base import ErrorCode, Tool, ToolParameter


class EnterWorktreeTool(Tool):
    """Create or enter a git worktree, switching the session's project root."""
    usage_notes = 'EnterWorktree: Switch to an isolated git worktree directory. All subsequent file operations target the worktree. Use for experimental changes without affecting the main branch.'

    def __init__(
        self,
        name: str = "EnterWorktree",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        worktree_manager: Optional[WorktreeManager] = None,
        code_agent: Optional[Any] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description='Create or enter a git worktree, switching the agent's working directory',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if worktree_manager is None:
            raise ValueError("worktree_manager is required")
        if code_agent is None:
            raise ValueError("code_agent is required")
        self._worktree_manager = worktree_manager
        self._code_agent = code_agent

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="name",
                type="string",
                description="Name for a new worktree. Creates wt/{name} branch.",
                required=False,
            ),
            ToolParameter(
                name="path",
                type="string",
                description="Absolute path of an existing worktree to re-enter.",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        wt_name = parameters.get("name")
        wt_path = parameters.get("path")

        has_name = isinstance(wt_name, str) and wt_name.strip()
        has_path = isinstance(wt_path, str) and wt_path.strip()

        if not has_name and not has_path:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Provide exactly one of 'name' (new worktree) or 'path' (existing worktree).",
                params_input=params_input,
            )
        if has_name and has_path:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Provide exactly one of 'name' or 'path', not both.",
                params_input=params_input,
            )

        try:
            if has_name:
                entry = self._worktree_manager.create(name=wt_name.strip())
            else:
                entry = self._worktree_manager.get_by_path(path=wt_path.strip())

            # Switch the session's project root to the worktree.
            self._code_agent.enter_worktree(name=entry["name"], path=entry["path"])

            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            return self.create_success_response(
                data={
                    "name": entry["name"],
                    "path": entry["path"],
                    "branch": entry.get("branch"),
                    "base_ref": entry.get("base_ref"),
                    "created_at": entry.get("created_at"),
                },
                text=(
                    f"Entered worktree '{entry['name']}' at {entry['path']}.\n"
                    f"Branch: {entry.get('branch', 'N/A')}. "
                    "All file operations now target this worktree."
                ),
                params_input=params_input,
                time_ms=elapsed_ms,
            )
        except WorktreeError as exc:
            return self.create_error_response(
                error_code=self._map_code(exc.code),
                message=exc.message,
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"EnterWorktree failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

    @staticmethod
    def _map_code(code: str) -> str:
        mapping = {
            "INVALID_PARAM": ErrorCode.INVALID_PARAM,
            "NOT_FOUND": ErrorCode.NOT_FOUND,
            "CONFLICT": ErrorCode.CONFLICT,
            "TIMEOUT": ErrorCode.TIMEOUT,
            "INTERNAL_ERROR": ErrorCode.INTERNAL_ERROR,
        }
        return mapping.get(code, ErrorCode.INTERNAL_ERROR)
