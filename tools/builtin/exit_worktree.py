"""ExitWorktree tool — exit the current worktree and restore project root."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import time
from pathlib import Path

from core.worktree.manager import WorktreeError, WorktreeManager
from prompts.tools_prompts.exit_worktree_prompt import exit_worktree_prompt
from ..base import ErrorCode, Tool, ToolParameter


class ExitWorktreeTool(Tool):
    """Exit the current worktree, restoring the original project root."""

    def __init__(
        self,
        name: str = "ExitWorktree",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        worktree_manager: Optional[WorktreeManager] = None,
        code_agent: Optional[Any] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description=exit_worktree_prompt,
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
                name="action",
                type="string",
                description="'keep' (preserve) or 'remove' (delete) the worktree.",
                required=True,
            ),
            ToolParameter(
                name="discard_changes",
                type="boolean",
                description="Force-remove even with uncommitted changes. Default false.",
                required=False,
                default=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        action = str(parameters.get("action", "") or "").strip().lower()
        discard_changes = bool(parameters.get("discard_changes", False))

        if action not in ("keep", "remove"):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'action' must be 'keep' or 'remove'.",
                params_input=params_input,
            )

        active = self._code_agent._active_worktree
        if active is None:
            return self.create_error_response(
                error_code=ErrorCode.CONFLICT,
                message="Not currently in a worktree. Nothing to exit.",
                params_input=params_input,
            )

        wt_name = active["name"]
        git_error: Optional[str] = None

        # Phase 1: git cleanup (may fail, but we always restore project_root).
        try:
            if self._worktree_manager.is_clean(wt_name):
                # Auto-remove clean worktrees regardless of action choice.
                self._worktree_manager.remove(wt_name)
                actual_action = "removed (auto — no changes)"
                merge_hint = ""
            elif action == "keep":
                self._worktree_manager.keep(wt_name)
                actual_action = "kept"
                branch = active.get("branch", f"wt/{wt_name}")
                merge_hint = (
                    f"Review: git diff {branch}\n"
                    f"Merge:  git merge {branch}\n"
                    f"Cleanup: git branch -D {branch}"
                )
            else:  # remove
                if not discard_changes:
                    return self.create_error_response(
                        error_code=ErrorCode.CONFLICT,
                        message=(
                            f"Worktree '{wt_name}' has uncommitted changes. "
                            "Use action='keep' to preserve them, or "
                            "discard_changes=true to force-remove."
                        ),
                        params_input=params_input,
                    )
                self._worktree_manager.remove(wt_name, discard_changes=True)
                actual_action = "removed (changes discarded)"
                merge_hint = ""
        except WorktreeError as exc:
            git_error = exc.message
            actual_action = f"{action} (git error)"
            merge_hint = ""

        # Phase 2: always restore project_root regardless of git outcome.
        try:
            self._code_agent.exit_worktree(action=action, discard_changes=discard_changes)
        except WorktreeError:
            pass

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        if git_error:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Worktree cleanup failed: {git_error}. Project root has been restored.",
                params_input=params_input,
                time_ms=elapsed_ms,
            )
        text = f"Exited worktree '{wt_name}' ({actual_action})."
        if merge_hint:
            text += f"\n{merge_hint}"

        return self.create_success_response(
            data={
                "previous_worktree": wt_name,
                "action": actual_action,
            },
            text=text,
            params_input=params_input,
            time_ms=elapsed_ms,
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
